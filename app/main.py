from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.core.config import get_settings
from app.features.batch_router import router as batch_router
from app.features.router import router as extract_router
from app.summary.router import router as summarise_router

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

settings = get_settings()
templates = Jinja2Templates(directory="app/templates")

# ---------------------------------------------------------------------------
# PaddleOCR results directory
# This is the `results/` folder written by app.py (the paddle_ocr service).
# Adjust this path if paddle_ocr runs from a different working directory.
# ---------------------------------------------------------------------------
_PADDLE_RESULTS_DIR = Path("results").resolve()


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    logger.info("Starting %s v%s", settings.app_name, settings.app_version)
    logger.info("LLM endpoint      : %s", settings.llm_url)
    logger.info("PaddleOCR service : %s", settings.paddle_ocr_base_url)
    logger.info("OCR results dir   : %s", _PADDLE_RESULTS_DIR)
    logger.info("Batch API         : POST /extract/batch (max %d files)", settings.max_files_per_batch)
    yield
    logger.info("Shutting down %s", settings.app_name)


app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    debug=settings.debug,
    lifespan=lifespan,
)

app.mount("/static", StaticFiles(directory="app/static"), name="static")
app.include_router(extract_router)
app.include_router(batch_router)
app.include_router(summarise_router)


@app.get("/", tags=["UI"])
async def home(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "request": request,
            "app_name": settings.app_name,
            "app_version": settings.app_version,
            "max_upload_mb": settings.max_upload_bytes // (1024 * 1024),
        },
    )


@app.get("/health", tags=["Meta"])
async def app_health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/ocr-download/{filename}", tags=["OCR"])
async def download_ocr_result(filename: str):
    """
    Serve a .txt file from the PaddleOCR results directory.

    The paddle_ocr service (app.py) writes results to its own `results/`
    folder. This endpoint lets the main app's UI download those files
    without exposing a separate port to the browser.

    Path traversal is prevented by resolving the path and confirming it
    remains inside _PADDLE_RESULTS_DIR.
    """
    # Prevent path traversal
    if ".." in filename or "/" in filename or "\\" in filename:
        raise HTTPException(status_code=400, detail="Invalid filename.")

    safe_name = "".join(c if c.isalnum() or c in "._- " else "_" for c in filename)
    file_path = (_PADDLE_RESULTS_DIR / safe_name).resolve()

    # Ensure the resolved path is still inside the results dir
    try:
        file_path.relative_to(_PADDLE_RESULTS_DIR)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid path.")

    if not file_path.exists():
        raise HTTPException(
            status_code=404,
            detail=f"OCR result file '{safe_name}' not found. "
                   "The PaddleOCR service may have already cleared it, "
                   "or the filename is incorrect.",
        )

    return FileResponse(
        path=str(file_path),
        media_type="text/plain",
        filename=safe_name,
    )


@app.exception_handler(Exception)
async def global_exception_handler(_, exc: Exception) -> JSONResponse:
    logger.exception("Unhandled exception: %s", exc)
    return JSONResponse(
        status_code=500,
        content={"success": False, "message": f"An unexpected error occurred: {exc}"},
    )