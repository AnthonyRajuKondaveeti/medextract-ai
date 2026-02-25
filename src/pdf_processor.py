"""
pdf_processor.py

PDF ingestion and per-page routing for the hybrid extraction pipeline.

Rendering strategy (key performance fix):
    Old: convert_from_bytes called once per page lazily (one expensive
         PDF parse per page that needed an image).
    New: After the text-extraction pass, collect ALL pages that need
         rendering, then call convert_from_bytes ONCE per PDF with the
         exact page range needed. Single PDF parse, batch PNG output.

Page modes:
    text  — pdfplumber extracted enough text (>= TEXT_MODE_MIN_CHARS chars)
    ocr   — scanned page; local OCR ran at pdf_processor time
    graph — graph/waveform page; marked PRESENT, never sent to AI
"""

import base64
import io
import logging
from dataclasses import dataclass, field
from typing import Optional

import pdfplumber
from pdf2image import convert_from_bytes
from PIL import Image

from config import (
    GRAPH_KEYWORDS,
    GRAPH_PAGE_MAX_CHARS,
    OCR_DPI,
    TEXT_MODE_MIN_CHARS,
)
from ocr_extractor import ocr_page_image, OcrResult

logger = logging.getLogger(__name__)


@dataclass
class PageResult:
    page_number: int
    mode: str           # "text", "ocr", "graph"
    raw_text: str = ""  # pdfplumber text (may be empty for ocr pages)
    text_length: int = 0
    image_base64: Optional[str] = None   # populated after batch render
    is_graph_page: bool = False
    graph_type: Optional[str] = None     # "ECG", "AUDIOGRAM", "TMT", "SPIROMETRY_CURVE"
    handler: Optional[str] = None        # set by batch_processor after routing

    # OCR results — populated for scanned (ocr-mode) pages only.
    ocr_text: str = ""
    ocr_confidence: float = 0.0
    ocr_engine: str = ""           # "tesseract" or "none"
    ocr_above_threshold: bool = False

    # Keep legacy alias so existing callers using .text still work.
    @property
    def text(self) -> str:
        return self.raw_text


@dataclass
class PDFProcessingResult:
    filename: str
    pages: list[PageResult] = field(default_factory=list)
    combined_text: str = ""
    ocr_images: list[dict] = field(default_factory=list)  # [{page_number, base64}]
    error: Optional[str] = None
    partial_ocr: bool = False


# ── Graph detection ────────────────────────────────────────────────────────────

_GRAPH_TYPE_MAP: list[tuple[list[str], str]] = [
    (["ecg", "ekg", "electrocardiogram"],           "ECG"),
    (["audiogram", "audiometry graph"],              "AUDIOGRAM"),
    (["tmt", "treadmill"],                           "TMT"),
    (["spirometry curve", "flow volume"],            "SPIROMETRY_CURVE"),
]


def detect_graph_page(text: str) -> tuple[bool, Optional[str]]:
    """
    Detect whether a page is primarily a graph/waveform page.

    Returns:
        (True, graph_type_str)  if a graph keyword is found
        (False, None)           otherwise
    """
    text_lower = text.lower()
    for keywords, label in _GRAPH_TYPE_MAP:
        if any(kw in text_lower for kw in keywords):
            return True, label
    for keyword in GRAPH_KEYWORDS:
        if keyword.lower() in text_lower:
            return True, "GRAPH"
    return False, None


# ── Image rendering ────────────────────────────────────────────────────────────

def _pil_to_base64(img: Image.Image) -> str:
    """Encode a PIL image to a base64 PNG string."""
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return base64.b64encode(buf.read()).decode("utf-8")


def _render_pages_batch(
    pdf_bytes: bytes,
    page_indices: list[int],
) -> dict[int, Image.Image]:
    """
    Render a specific set of page indices from a PDF in ONE convert_from_bytes
    call.  Returns {page_index: PIL.Image}.

    convert_from_bytes accepts first_page/last_page (1-based), so we render
    the minimal contiguous range that covers all requested indices, then keep
    only the ones we actually need.  For non-contiguous sets (e.g. pages 1, 5,
    9) this renders a few extra pages but still beats N separate calls because
    the PDF is parsed only once.

    For fully contiguous or near-contiguous sets (the common case when many
    pages need OCR) this is essentially free vs the old approach.
    """
    if not page_indices:
        return {}

    first = min(page_indices) + 1   # convert_from_bytes is 1-based
    last  = max(page_indices) + 1

    try:
        images = convert_from_bytes(
            pdf_bytes,
            dpi=OCR_DPI,
            first_page=first,
            last_page=last,
        )
    except Exception as exc:
        logger.warning(f"Batch render failed (pages {first}–{last}): {exc}")
        return {}

    # images[0] corresponds to first_page, images[1] to first_page+1, etc.
    result: dict[int, Image.Image] = {}
    for page_index in page_indices:
        offset = page_index - (first - 1)   # convert back to 0-based offset
        if 0 <= offset < len(images):
            result[page_index] = images[offset]

    return result


# ── Main entry point ───────────────────────────────────────────────────────────

def process_pdf(filename: str, pdf_bytes: bytes) -> PDFProcessingResult:
    """
    Process a single PDF and return per-page extraction results.

    Pass 1 — text extraction (pdfplumber, cheap):
        For each page, extract text and classify as text/ocr/graph.
        Graph pages are fully handled here (marked PRESENT).
        OCR pages: render + local OCR happens in the batch render below.
        Text pages: record that they may need rendering later.

    Pass 2 — batch image render (one convert_from_bytes call per PDF):
        Collect all page indices that need a PNG (OCR pages + text pages
        that will likely need AI).  Render them all in one shot.
        Assign image_base64 to the relevant PageResult objects.

    Returns PDFProcessingResult with pages, combined_text, error.
    """
    result = PDFProcessingResult(filename=filename)
    text_parts: list[str] = []

    # page_index → PageResult for pages that need batch rendering
    pages_needing_render: list[int] = []

    # ── Pass 1: Text extraction ────────────────────────────────────────────────
    try:
        pdf_file = io.BytesIO(pdf_bytes)
        with pdfplumber.open(pdf_file) as pdf:

            if pdf.metadata and pdf.metadata.get("Encrypt"):
                result.error = "PDF_PASSWORD_PROTECTED"
                return result

            total_pages = len(pdf.pages)
            if total_pages == 0:
                result.error = "PDF_CORRUPTED"
                return result

            for page_index, page in enumerate(pdf.pages):
                page_number = page_index + 1

                try:
                    raw_text = page.extract_text() or ""
                except Exception as exc:
                    logger.warning(
                        f"[{filename}] pdfplumber failed on page {page_number}: {exc}"
                    )
                    raw_text = ""

                stripped_text = raw_text.strip()
                text_length   = len(stripped_text)

                # ── Graph page ─────────────────────────────────────────────────
                is_graph, graph_type = detect_graph_page(raw_text)
                if text_length < GRAPH_PAGE_MAX_CHARS and is_graph:
                    page_result = PageResult(
                        page_number=page_number,
                        mode="graph",
                        raw_text=stripped_text,
                        text_length=text_length,
                        is_graph_page=True,
                        graph_type=graph_type,
                        handler="GRAPH_PAGE",
                    )
                    result.pages.append(page_result)
                    # Graph pages get PRESENT/NOT PRESENT only — no text content needed
                    label = f"[PAGE {page_number} - GRAPH: {graph_type}]"
                    text_parts.append(label + "\nResult: PRESENT")
                    continue

                # ── Text page ──────────────────────────────────────────────────
                if text_length >= TEXT_MODE_MIN_CHARS:
                    page_result = PageResult(
                        page_number=page_number,
                        mode="text",
                        raw_text=stripped_text,
                        text_length=text_length,
                        # image_base64 filled by Pass 2
                    )
                    result.pages.append(page_result)
                    text_parts.append(f"[PAGE {page_number}]\n{stripped_text}")
                    pages_needing_render.append(page_index)
                    continue

                # ── OCR page ───────────────────────────────────────────────────
                # Image render happens in Pass 2 — just record the index for now.
                page_result = PageResult(
                    page_number=page_number,
                    mode="ocr",
                    raw_text=stripped_text,
                    text_length=text_length,
                    handler="CLAUDE_HANDLED",   # may be revised by batch_processor
                )
                result.pages.append(page_result)
                text_parts.append(f"[PAGE {page_number} - IMAGE/OCR]")
                pages_needing_render.append(page_index)

    except pdfplumber.pdfminer.pdfparser.PDFSyntaxError as exc:
        logger.error(f"[{filename}] PDF syntax error: {exc}")
        result.error = "PDF_CORRUPTED"
        return result
    except Exception as exc:
        logger.error(f"[{filename}] Unexpected error during PDF processing: {exc}")
        exc_str = str(exc).lower()
        if "password" in exc_str or "encrypt" in exc_str:
            result.error = "PDF_PASSWORD_PROTECTED"
        else:
            result.error = "PDF_CORRUPTED"
        return result

    # ── Pass 2: Batch image render ─────────────────────────────────────────────
    # One convert_from_bytes call for the entire PDF — major speed win vs the
    # old approach of one call per page that needed an image.
    if pages_needing_render:
        pages_needing_render_set = set(pages_needing_render)
        page_index_to_result: dict[int, PageResult] = {
            pr.page_number - 1: pr for pr in result.pages
            if pr.page_number - 1 in pages_needing_render_set
        }

        rendered = _render_pages_batch(pdf_bytes, pages_needing_render)
        failed_ocr_pages: list[int] = []

        for page_index, pil_image in rendered.items():
            pr = page_index_to_result.get(page_index)
            if pr is None:
                continue

            b64 = _pil_to_base64(pil_image)
            pr.image_base64 = b64

            if pr.mode == "ocr":
                # Run local OCR on the rendered image
                try:
                    ocr_result: OcrResult = ocr_page_image(pil_image)
                    pr.ocr_text           = ocr_result.text
                    pr.ocr_confidence     = ocr_result.confidence
                    pr.ocr_engine         = ocr_result.engine
                    pr.ocr_above_threshold = ocr_result.above_threshold
                except Exception as exc:
                    logger.warning(
                        f"[{filename}] OCR failed on page {pr.page_number}: {exc}"
                    )

                result.ocr_images.append({
                    "page_number": pr.page_number,
                    "base64": b64,
                })

        # Pages that were requested but not returned by the renderer
        rendered_indices = set(rendered.keys())
        for page_index in pages_needing_render:
            pr = page_index_to_result.get(page_index)
            if pr and page_index not in rendered_indices and pr.mode == "ocr":
                failed_ocr_pages.append(pr.page_number)
                logger.warning(
                    f"[{filename}] Page render failed for page {pr.page_number}"
                )

        if failed_ocr_pages:
            result.partial_ocr = True
            logger.warning(
                f"[{filename}] Partial OCR — failed pages: {failed_ocr_pages}"
            )

    result.combined_text = "\n\n".join(text_parts)
    return result
