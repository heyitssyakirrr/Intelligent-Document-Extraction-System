from __future__ import annotations

import logging

from fastapi import APIRouter, File, UploadFile
from fastapi.responses import JSONResponse

from app.core.config import get_settings
from app.features.extraction.concurrency import _ocr_queue, pending_task_count
from app.services.file_service import validate_and_read_upload

logger = logging.getLogger(__name__)
settings = get_settings()

router = APIRouter(prefix="/extract", tags=["Single Extraction"])


@router.post(
    "/single",
    summary="Submit & Forget — queue one PDF for background OCR → LLM extraction",
    response_class=JSONResponse,
    responses={
        200: {
            "description": "File accepted and queued. Connection closes immediately.",
            "content": {"application/json": {"example": {"success": True, "message": "File queued successfully."}}},
        },
        422: {
            "description": "Validation failure (unsupported type, file too large, etc.).",
            "content": {"application/json": {"example": {"success": False, "message": "<reason>"}}},
        },
        503: {
            "description": "Server is at capacity — too many files already queued/processing.",
            "content": {"application/json": {"example": {"success": False, "message": "Server is at capacity. Please retry shortly."}}},
        },
    },
)
async def extract_single(
    file: UploadFile = File(..., description="Single PDF file to process"),
) -> JSONResponse:
    if pending_task_count() >= settings.single_max_pending_tasks:
        logger.warning(
            "Rejecting upload '%s' — at capacity (%d/%d in-flight tasks)",
            file.filename, pending_task_count(), settings.single_max_pending_tasks,
        )
        return JSONResponse(
            status_code=503,
            content={
                "success": False,
                "message": "Server is at capacity. Please retry shortly.",
            },
        )

    try:
        pdf_bytes, _ext = await validate_and_read_upload(file)
    except Exception as exc:
        logger.warning("Validation failed for '%s': %s", file.filename, exc)
        return JSONResponse(
            status_code=getattr(exc, "status_code", 422),
            content={"success": False, "message": getattr(exc, "detail", str(exc))},
        )

    filename = file.filename or "uploaded_file.pdf"
    logger.info("File accepted and queued for background processing: '%s'", filename)

    await _ocr_queue.put((pdf_bytes, filename))
    logger.info("File queued for OCR: '%s' (queue size now ~%d)", filename, _ocr_queue.qsize())

    return JSONResponse(
        status_code=200,
        content={"success": True, "message": "File queued successfully."},
    )