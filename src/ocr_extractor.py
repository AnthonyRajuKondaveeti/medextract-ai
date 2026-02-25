"""
ocr_extractor.py

Local OCR layer — sits between pdfplumber text extraction and OpenAI in the pipeline.

Pipeline position:
    pdfplumber text → regex
        → if scanned page (text ≤ 100 chars):
            local OCR (Tesseract only)
                → if OCR confidence ≥ threshold: regex on OCR text
                    → if regex ≥ 3 fields: DONE  (free, no OpenAI)
                    → else: OpenAI
                → if OCR confidence < threshold: OpenAI directly

Public API:
    ocr_page_image(pil_image) → OcrResult
        Returns extracted text, confidence score (0.0–1.0), and engine used.

Confidence is computed as the mean per-word confidence from Tesseract.
If confidence < OCR_CONFIDENCE_THRESHOLD (from config), the caller should skip
regex and go straight to OpenAI — low-confidence OCR fed into regex produces
silent wrong extractions, which are worse than an LLM miss.

Note: PaddleOCR removed for lighter, faster builds. OpenAI handles low-confidence cases.
"""

import logging
from dataclasses import dataclass
from typing import Optional

from PIL import Image

from config import OCR_CONFIDENCE_THRESHOLD

logger = logging.getLogger(__name__)


# ── Result dataclass ───────────────────────────────────────────────────────────

@dataclass
class OcrResult:
    text: str                  # Extracted text, ready to feed into regex_extractor
    confidence: float          # Mean confidence 0.0–1.0
    engine: str                # "tesseract" or "none"
    above_threshold: bool      # confidence >= OCR_CONFIDENCE_THRESHOLD


# ── Tesseract extraction ───────────────────────────────────────────────────────

def _ocr_with_tesseract(image: Image.Image) -> OcrResult:
    """
    Run Tesseract OCR on a PIL image.

    Uses pytesseract.image_to_data() to get per-word confidence scores,
    then reconstructs text line-by-line preserving layout for regex.

    Tesseract confidence is 0–100; we normalise to 0.0–1.0.
    Words with conf == -1 (non-text segments) are excluded from the mean.
    """
    try:
        import pytesseract
        from pytesseract import Output
    except ImportError as exc:
        raise RuntimeError(f"pytesseract not installed: {exc}") from exc

    try:
        # PSM 6: Assume a single uniform block of text — best for lab report pages
        custom_config = r"--oem 3 --psm 6"
        data = pytesseract.image_to_data(
            image,
            config=custom_config,
            output_type=Output.DICT,
        )
    except Exception as exc:
        raise RuntimeError(f"Tesseract inference failed: {exc}") from exc

    words: list[str] = []
    confidences: list[float] = []
    lines: dict[tuple, list[str]] = {}   # (block_num, par_num, line_num) → [words]

    n_boxes = len(data["text"])
    for i in range(n_boxes):
        word = data["text"][i].strip()
        conf = int(data["conf"][i])

        if conf == -1 or not word:   # non-text segment or empty
            continue

        key = (data["block_num"][i], data["par_num"][i], data["line_num"][i])
        lines.setdefault(key, []).append(word)
        confidences.append(conf / 100.0)   # normalise to 0.0–1.0

    # Reconstruct text preserving line breaks for regex patterns
    extracted_text = "\n".join(
        " ".join(words) for words in lines.values()
    )

    mean_confidence = sum(confidences) / len(confidences) if confidences else 0.0
    above = mean_confidence >= OCR_CONFIDENCE_THRESHOLD

    logger.debug(
        f"Tesseract: {len(confidences)} words, confidence={mean_confidence:.3f}, "
        f"above_threshold={above}"
    )

    return OcrResult(
        text=extracted_text,
        confidence=round(mean_confidence, 4),
        engine="tesseract",
        above_threshold=above,
    )


# ── Public entry point ─────────────────────────────────────────────────────────

def ocr_page_image(image: Image.Image) -> OcrResult:
    """
    Run local OCR on a PIL image using Tesseract only.

    Strategy:
        1. Run Tesseract OCR on the image.
        2. If confidence ≥ OCR_CONFIDENCE_THRESHOLD → return it (caller runs regex).
        3. If confidence < threshold → return it (caller skips regex, goes to OpenAI).
        4. If Tesseract fails → return empty OcrResult (engine="none").

    The caller (batch_processor) decides what to do with the result:
        - above_threshold=True  → run regex; go to OpenAI only if regex < 3 fields.
        - above_threshold=False → skip regex; go straight to OpenAI.

    Args:
        image: PIL.Image.Image — page rendered at OCR_DPI (from pdf_processor).

    Returns:
        OcrResult with text, confidence, engine, above_threshold.
    """
    try:
        result = _ocr_with_tesseract(image)
        logger.info(
            f"OCR: Tesseract result (conf={result.confidence:.3f}, "
            f"above_threshold={result.above_threshold})."
        )
        return result
    except Exception as exc:
        logger.error(f"OCR: Tesseract failed ({exc}). Returning empty result.")
        return OcrResult(text="", confidence=0.0, engine="none", above_threshold=False)
