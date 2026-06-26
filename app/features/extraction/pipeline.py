from __future__ import annotations

import asyncio
import logging
import random
import tempfile
from pathlib import Path

from app.core.config import get_settings
from app.features.extraction.concurrency import (
    _llm_semaphore,
    _ocr_queue,
    _track_task,
)
from app.features.extraction.storage import append_failure_row, append_success_row, append_ocr_output
from app.features.prompt import build_extraction_prompt
from app.models.schemas import ExtractionResult
from app.services.llm_client import LLMClient
from app.services.paddle_ocr import process_pdf

logger = logging.getLogger(__name__)
settings = get_settings()

_llm_client = LLMClient()


async def _run_ocr(pdf_bytes: bytes, filename: str) -> str:
    suffix = Path(filename).suffix or ".pdf"
    tmp_path: Path | None = None

    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            tmp.write(pdf_bytes)
            tmp_path = Path(tmp.name)

        loop = asyncio.get_running_loop()

        text: str = await asyncio.wait_for(
            loop.run_in_executor(None, process_pdf, str(tmp_path)),
            timeout=settings.ocr_timeout_seconds,
        )

        logger.debug("OCR completed for '%s': %d characters extracted", filename, len(text))
        return text

    finally:
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)


async def _run_llm_extraction(ocr_text: str, filename: str) -> ExtractionResult:
    prompt = build_extraction_prompt(ocr_text)
    llm_result = await _llm_client.extract_fields(
        prompt,
        stop=[
            "} {", "\n} {", "\n}{", "}\n{", "}\r\n{",
            "}\n\n", "}\r\n\r\n", "}\n ", "} \n",
            "}\n#", "}\n`", "\n}\n ", "\n}\n#",
            "\n}\n`", "\n}\n\n", "\n}\r\n\r\n",
        ],
    )
    return ExtractionResult(
        name=llm_result.get("name"),
        master_account_number=llm_result.get("master_account_number"),
        sub_account_number=llm_result.get("sub_account_number"),
        address=llm_result.get("address"),
        fi_num=llm_result.get("fi_num"),
        bank_name=llm_result.get("bank_name"),
    )


async def _llm_stage(ocr_text: str, filename: str, pdf_bytes: bytes) -> None:
    last_exc: Exception | None = None
    total_attempts = settings.llm_max_retries + 1

    for attempt in range(total_attempts):
        if attempt > 0:
            backoff = settings.llm_retry_base_backoff * (2 ** (attempt - 1))
            jitter = random.uniform(0.0, 1.0)
            wait = backoff + jitter
            logger.warning(
                "LLM retry %d/%d for '%s' — sleeping %.1fs",
                attempt, settings.llm_max_retries, filename, wait,
            )
            await asyncio.sleep(wait)

        async with _llm_semaphore:
            logger.debug(
                "LLM semaphore acquired for '%s' (attempt %d/%d)",
                filename, attempt + 1, total_attempts,
            )
            try:
                result = await _run_llm_extraction(ocr_text, filename)
            except Exception as exc:
                last_exc = exc
                logger.warning(
                    "LLM attempt %d/%d failed for '%s': %s",
                    attempt + 1, total_attempts, filename, exc,
                )
                continue

        logger.info(
            "LLM extraction succeeded for '%s' (attempt %d/%d)",
            filename, attempt + 1, total_attempts,
        )
        await append_success_row(result, filename)
        return

    msg = f"LLM failed after {total_attempts} attempt(s): {last_exc}"
    logger.error("LLM permanently failed for '%s': %s", filename, last_exc, exc_info=True)
    await append_failure_row(pdf_bytes, filename, msg)


async def _ocr_worker() -> None:
    while True:
        pdf_bytes, filename = await _ocr_queue.get()
        logger.info("OCR worker picked up '%s'", filename)

        try:
            ocr_text = await _run_ocr(pdf_bytes, filename)

        except asyncio.TimeoutError:
            msg = f"OCR timed out after {settings.ocr_timeout_seconds}s"
            logger.error("OCR timeout for '%s': %s", filename, msg)
            await append_failure_row(pdf_bytes, filename, msg)
            _ocr_queue.task_done()
            continue

        except Exception as exc:
            msg = f"OCR failed: {exc}"
            logger.error("OCR error for '%s': %s", filename, exc, exc_info=True)
            await append_failure_row(pdf_bytes, filename, msg)
            _ocr_queue.task_done()
            continue

        if not ocr_text or not ocr_text.strip():
            msg = "OCR returned empty text — no content extracted from PDF"
            logger.warning("Empty OCR output for '%s'", filename)
            await append_failure_row(pdf_bytes, filename, msg)
            _ocr_queue.task_done()
            continue

        try:
            await append_ocr_output(ocr_text, filename)
        except Exception as exc:
            logger.warning("Failed to persist OCR output for '%s': %s", filename, exc)

        failure_bytes = pdf_bytes
        pdf_bytes = None

        task = asyncio.create_task(
            _llm_stage(ocr_text, filename, failure_bytes),
            name=f"llm_stage:{filename}",
        )
        _track_task(task)
        _ocr_queue.task_done()


_ocr_worker_task: asyncio.Task | None = None


def start_ocr_worker() -> None:
    global _ocr_worker_task
    _ocr_worker_task = asyncio.create_task(_ocr_worker(), name="ocr_worker")
    logger.info("Single-file OCR worker started.")