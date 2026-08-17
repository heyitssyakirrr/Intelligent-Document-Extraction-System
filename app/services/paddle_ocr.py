from __future__ import annotations

import os
os.environ["FLAGS_use_mkldnn"] = "0"

import time
import logging
import math
from pathlib import Path

logger = logging.getLogger(__name__)

_ocr_instance = None


def _get_ocr():
    global _ocr_instance
    if _ocr_instance is not None:
        return _ocr_instance

    try:
        from paddleocr import PaddleOCR
    except ImportError:
        raise RuntimeError("PaddleOCR not installed.")

    # For air-gapped environments: use pre-downloaded models from local directory.
    # __file__ = <project_root>/app/services/paddle_ocr.py
    # .parent       = app/services/
    # .parent.parent = app/
    # .parent.parent.parent = <project_root>/
    model_base = Path(__file__).resolve().parent.parent.parent / "models"
    det_dir = model_base / "en_PP-OCRv3_det_infer"
    rec_dir = model_base / "en_PP-OCRv3_rec_infer"
    cls_dir = model_base / "ch_ppocr_mobile_v2.0_cls_infer"

    local_kwargs = {}
    if det_dir.exists() and rec_dir.exists():
        logger.info("Using local models from %s", model_base)
        local_kwargs = {
            "det_model_dir": str(det_dir),
            "rec_model_dir": str(rec_dir),
            "cls_model_dir": str(cls_dir) if cls_dir.exists() else None,
        }
        local_kwargs = {k: v for k, v in local_kwargs.items() if v is not None}

    logger.info("Loading PP-OCRv5 model (CPU)...")
    _ocr_instance = PaddleOCR(
        lang="en",
        use_doc_orientation_classify=False,
        use_doc_unwarping=False,
        use_angle_cls=True,
        use_textline_orientation=False,
        text_det_thresh=0.3,
        text_det_box_thresh=0.5,
        text_det_unclip_ratio=1.8,
        text_recognition_batch_size=6,
        text_rec_score_thresh=0.0,
        enable_mkldnn=False,
        **local_kwargs,
    )
    logger.info("Model loaded.")
    return _ocr_instance


def _pdf_to_images(pdf_path: str, dpi: int = 300) -> list[tuple[int, object]]:
    """Render every page of a PDF to a numpy array using pypdfium2."""
    try:
        import pypdfium2 as pdfium
    except ImportError:
        raise RuntimeError("pypdfium2 not installed. Run: pip install pypdfium2")

    import numpy as np

    doc = pdfium.PdfDocument(pdf_path)
    scale = dpi / 72  # pypdfium2 default is 72 DPI
    images = []

    for page_num, page in enumerate(doc, start=1):
        bitmap = page.render(scale=scale, rotation=0)
        img_array = bitmap.to_numpy()

        # Drop alpha channel if present (RGBA -> RGB)
        if img_array.shape[2] == 4:
            img_array = img_array[:, :, :3]

        images.append((page_num, img_array))
        logger.debug("Page %d rendered at %d DPI (%dx%d px)",
                     page_num, dpi, img_array.shape[1], img_array.shape[0])

    doc.close()
    return images

def _auto_deskew_image(img_array, max_skew_degrees: float = 10.0):
    """
    Automatically correct small page skew before OCR.

    This is intended for scanned PDFs where the page is tilted slightly left/right.
    It only rotates when the detected angle is realistic and safe.
    """
    try:
        import cv2
        import numpy as np
    except ImportError:
        logger.warning("OpenCV not installed; skipping deskew.")
        return img_array

    if img_array is None:
        return img_array

    original = img_array

    try:
        gray = cv2.cvtColor(img_array, cv2.COLOR_RGB2GRAY)

        # Invert threshold so text becomes white on black background.
        gray = cv2.bitwise_not(gray)
        thresh = cv2.threshold(
            gray,
            0,
            255,
            cv2.THRESH_BINARY | cv2.THRESH_OTSU,
        )[1]

        coords = np.column_stack(np.where(thresh > 0))
        if coords.size == 0:
            logger.debug("Deskew skipped: no foreground pixels found.")
            return original

        angle = cv2.minAreaRect(coords)[-1]

        # OpenCV returns angles in a slightly awkward range.
        if angle < -45:
            angle = -(90 + angle)
        else:
            angle = -angle

        if not math.isfinite(angle):
            logger.debug("Deskew skipped: invalid angle.")
            return original

        # Guardrails: only correct small scan skew.
        if abs(angle) < 0.3:
            logger.debug("Deskew skipped: angle %.2f is too small.", angle)
            return original

        if abs(angle) > max_skew_degrees:
            logger.warning(
                "Deskew skipped: detected angle %.2f exceeds safe limit %.2f.",
                angle,
                max_skew_degrees,
            )
            return original

        height, width = img_array.shape[:2]
        center = (width // 2, height // 2)

        matrix = cv2.getRotationMatrix2D(center, angle, 1.0)

        deskewed = cv2.warpAffine(
            img_array,
            matrix,
            (width, height),
            flags=cv2.INTER_CUBIC,
            borderMode=cv2.BORDER_REPLICATE,
        )

        logger.info("Deskew applied: %.2f degrees", angle)
        return deskewed

    except Exception as exc:
        logger.warning("Deskew failed; using original image: %s", exc)
        return original


def _extract_bbox_info(bbox_coords: list) -> tuple[float, float, float, float]:
    """Extract bounding box coordinates from PaddleOCR format.
    
    PaddleOCR returns bbox as: [[x1,y1], [x2,y2], [x3,y3], [x4,y4]]
    Returns: (min_x, min_y, max_x, max_y) for easier row/column grouping.
    """
    if not bbox_coords or len(bbox_coords) < 2:
        return 0, 0, 0, 0
    
    xs = [coord[0] for coord in bbox_coords]
    ys = [coord[1] for coord in bbox_coords]
    return min(xs), min(ys), max(xs), max(ys)


def _group_text_by_rows(page_results: list, y_threshold: float = 15) -> list[list[tuple[float, str]]]:
    """Group detected text elements into rows based on Y-coordinate similarity.
    
    Args:
        page_results: PaddleOCR results for one page
        y_threshold: Maximum Y difference (in pixels) to consider text on same row
    
    Returns:
        List of rows, where each row is a list of (x_position, text) tuples sorted left-to-right.
    """
    # Collect all text with position info
    text_elements: list[tuple[float, float, str]] = []
    
    for line in page_results:
        if line is None or len(line) < 2:
            continue
        
        bbox = line[0]
        text_info = line[1]
        
        if bbox is None or text_info is None:
            continue
        
        min_x, min_y, max_x, max_y = _extract_bbox_info(bbox)
        text = str(text_info[0]).strip()
        
        if text:
            # Use midpoint of Y coordinates as the row indicator
            y_mid = (min_y + max_y) / 2
            x_mid = (min_x + max_x) / 2
            text_elements.append((y_mid, x_mid, text))
    
    if not text_elements:
        return []
    
    # Sort by Y position to process rows top-to-bottom
    text_elements.sort(key=lambda t: t[0])
    
    # Group into rows based on Y proximity
    rows: list[list[tuple[float, str]]] = []
    current_row: list[tuple[float, str]] = []
    current_y = None
    
    for y_mid, x_mid, text in text_elements:
        if current_y is None:
            current_y = y_mid
            current_row.append((x_mid, text))
        elif abs(y_mid - current_y) <= y_threshold:
            # Same row
            current_row.append((x_mid, text))
        else:
            # New row detected
            if current_row:
                rows.append(current_row)
            current_row = [(x_mid, text)]
            current_y = y_mid
    
    # Add the last row
    if current_row:
        rows.append(current_row)
    
    # Sort each row left-to-right by X coordinate
    for row in rows:
        row.sort(key=lambda t: t[0])
    
    return rows


def _format_markdown_row(row: list[tuple[float, str]]) -> str:
    texts = [text.replace("|", "\\|").replace("\n", " ").strip() for _, text in row]
    texts = [text for text in texts if text]
    if len(texts) == 1:
        return texts[0]
    return f"| {' | '.join(texts)} |"


def _format_page_as_markdown(results: list, page_num: int) -> str:
    page_lines: list[str] = []
    for page_result in results:
        if page_result is None:
            continue
        for row in _group_text_by_rows(page_result):
            markdown_row = _format_markdown_row(row)
            if markdown_row:
                page_lines.append(markdown_row)

    if not page_lines:
        return ""
    return f"# Page {page_num}\n\n" + "\n".join(page_lines)


def process_pdf(pdf_path: str, dpi: int = 300) -> str:
    pdf_path = str(pdf_path)
    logger.info("Processing: %s at %d DPI", pdf_path, dpi)

    images = _pdf_to_images(pdf_path, dpi=dpi)
    ocr = _get_ocr()

    markdown_pages: list[str] = []
    t0 = time.time()

    for page_num, img_array in images:
        logger.debug("OCR on page %d...", page_num)

        img_array =  _auto_deskew_image(img_array)

        try:
            results = ocr.ocr(img_array, cls=True)
        except Exception as exc:
            logger.warning("Page %d OCR error: %s", page_num, exc)
            continue

        if not results:
            logger.debug("Page %d: no text detected.", page_num)
            continue

        markdown_page = _format_page_as_markdown(results, page_num)
        if markdown_page:
            markdown_pages.append(markdown_page)

    elapsed = time.time() - t0
    line_count = sum(page.count("\n") + 1 for page in markdown_pages)
    logger.info("Done in %.1fs — %d Markdown lines extracted", elapsed, line_count)
    return "\n\n".join(markdown_pages)
