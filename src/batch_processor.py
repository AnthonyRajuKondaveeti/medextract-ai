"""
batch_processor.py

Async orchestration layer for the hybrid PDF extraction pipeline.

Per-page routing (strict handoff — no overlap between regex and AI):
    A. Graph page  → mark PRESENT/NOT PRESENT, done. AI never called.
    B. OCR page    → local OCR ran in pdf_processor.
                     If confidence >= threshold AND regex >= 3 fields
                       → OCR_HANDLED (free, AI never called).
                     Else → AI call with page image.
    C. Text page   → run regex.
                     If regex >= 3 fields → REGEX_HANDLED (free, AI never called).
                     Else → AI call with page image (or text fallback).

The strict handoff means regex and AI results never overlap on the same field.
There are no conflicts to resolve and no confidence scoring is needed.

Concurrency model:
    _process_single_file is async — AI calls (I/O bottleneck) are awaited.
    PDF processing and OCR (CPU-bound) run in asyncio.to_thread.
    MAX_CONCURRENT_EXTRACTIONS controls files in-flight simultaneously.
    AI_CONCURRENCY (in ai_extractor) caps concurrent OpenAI requests globally.

Phase 2 chunking strategy:
    Rather than one AI call per pending page, pending pages are grouped into
    chunks before firing.  Each chunk = one extract_with_ai() call, so the
    ~700-token SYSTEM_PROMPT is paid once per chunk instead of once per page.

    Chunk sizes (tuned for GPT-4o vision accuracy vs. cost):
        IMAGE_CHUNK_SIZE = 3  — 3 images per call; keeps vision attention sharp
                                 and stays well inside the OpenAI image limit.
        TEXT_CHUNK_SIZE  = 4  — text-only calls tolerate slightly larger context.

    For a 9-page doc where 6 pages reach AI:
        Old: 6 calls  (6 × SYSTEM_PROMPT)
        New: 2–3 calls (2–3 × SYSTEM_PROMPT)  ≈ 60–65% token reduction.

    Chunks still fire concurrently via asyncio.gather — latency is unchanged
    (same wall-clock time, fewer network round-trips).

    All existing behaviour is preserved:
        • Phase 1 is untouched (regex / OCR / graph routing).
        • null_fields_snapshot taken after all Phase 1 merges.
        • Per-chunk null-field pruning for text mode.
        • Cost logging: one openai_calls row per chunk (not per page).
        • Merge logic (_merge_into_patient) unchanged.
        • pages_ai_handled incremented once per page inside the chunk.
"""

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional

from config import MAX_CONCURRENT_EXTRACTIONS
from _db import (
    init_db,
    db_create_job,
    db_set_job_status,
    db_save_excel,
    db_update_file,
    db_get_job,
    db_get_excel,
    db_get_files,
    db_insert_openai_call,
)
from pdf_processor import process_pdf
from regex_extractor import extract_with_regex, count_regex_fields
from ai_extractor import extract_with_ai
from validator import validate_and_clean, count_fields, MASTER_COLUMNS, FLAG_FIELDS
from excel_writer import build_excel, get_output_filename

logger = logging.getLogger(__name__)

# ── Cost estimation ────────────────────────────────────────────────────────────
# GPT-4o pricing: $2.50 / 1M input tokens, $10 / 1M output tokens
_COST_PER_INPUT_TOKEN  = 2.5  / 1_000_000
_COST_PER_OUTPUT_TOKEN = 10.0 / 1_000_000


def _estimate_cost(input_tokens: int, output_tokens: int) -> float:
    return round(
        input_tokens  * _COST_PER_INPUT_TOKEN +
        output_tokens * _COST_PER_OUTPUT_TOKEN,
        8,
    )


# ── Job status constants ───────────────────────────────────────────────────────

STATUS_PENDING    = "pending"
STATUS_PROCESSING = "processing"
STATUS_DONE       = "done"
STATUS_FAILED     = "failed"
STATUS_COMPLETE   = "complete"   # job-level: all files finished

# ── Phase 2 chunk sizes ────────────────────────────────────────────────────────
# IMAGE chunks: 3 pages per call.
#   - GPT-4o vision attention degrades noticeably beyond 3–4 simultaneous images.
#   - Stays comfortably within OpenAI's per-call image limit.
#   - For a 9-page doc with 6 OCR pages: 2 image chunks = 2 AI calls.
#
# TEXT chunks: 4 pages per call.
#   - No image-count ceiling; slightly larger context is fine for text-only calls.
#   - Combined text of 4 lab report pages ≈ 2,000–3,500 tokens — model handles well.
#   - For a 9-page doc with 6 text pages: 2 text chunks = 2 AI calls.
_IMAGE_CHUNK_SIZE = 3
_TEXT_CHUNK_SIZE  = 4

# ── Field category sets ────────────────────────────────────────────────────────

# Narrative fields: free text that only AI can reliably extract.
# These are never returned by regex so no merge conflict is possible.
_NARRATIVE_FIELDS = {
    "XRAY", "PFT", "AUDIOMETRY", "Suggestion", "Remarks", "Mobile",
    "Urine_Colour", "Urine_Transparency", "Urine_PH",
    "Urine_Protein_Albumin", "Urine_Glucose", "Urine_Bilirubin",
    "Urine_Blood", "Urine_RBC",
    "Urine_Casts", "Urine_Crystals",
    "Urine_Specific_Gravity",
}

# All value fields (excludes _Flag and meta columns)
_ALL_VALUE_FIELDS = [
    col for col in MASTER_COLUMNS
    if not col.endswith("_Flag")
    and col not in ("Extraction_Note", "Data_Quality")
]

# Fields excluded from unrecovered-field reporting (genuinely optional)
_SKIP_REPORT_FIELDS = {
    "Mobile", "Remarks", "Suggestion", "EmpCode", "UHIDNo",
}

# ── Per-page null-field pruning ────────────────────────────────────────────────
# For text pages: if none of a field's known aliases appear in the page text,
# that field cannot be on this page — skip asking AI for it.
# Graph/image fields are ALWAYS kept (content may be visual, not textual).
# Narrative fields with no reliable alias are also always kept.
#
# Aliases are lowercase substrings — any match triggers inclusion.
# Kept deliberately broad: false positives (keeping a field) are harmless;
# false negatives (dropping a field that IS present) waste a field forever.

_ALWAYS_KEEP_FIELDS = {
    # Visual fields — content not in raw_text for scanned pages
    "XRAY", "PFT", "AUDIOMETRY",
    # Free-text narrative — no reliable single-keyword alias
    "Remarks", "Suggestion",
    # Identity fields that may appear anywhere / in headers
    "PatientName", "Mobile", "EmpCode", "UHIDNo",
    # Lab name / report date — can appear on any page header
    "Lab_Name", "Report_Date",
}

_FIELD_ALIASES: dict[str, list[str]] = {
    # Demographics
    "Age":                  ["age"],
    "Gender":               ["gender", "sex"],
    "Height":               ["height", " ht "],
    "Weight":               ["weight", " wt "],
    "BMI":                  ["bmi"],
    "BP":                   ["bp", "blood pressure"],
    "Pulse":                ["pulse", " pr "],
    # Blood group
    "Blood_Group":          ["blood group", "abo group", "blood grp"],
    "Rh_Type":              ["rh ", "rhesus"],
    # CBC red cell
    "Haemoglobin":          ["haemoglobin", "hemoglobin", "hb%", " hb "],
    "Red_Blood_Cell_Count": ["rbc", "r.b.c", "red blood cell"],
    "Hct":                  ["hct", "pcv", "p.c.v", "haematocrit", "hematocrit"],
    "MCV":                  ["mcv"],
    "MCH":                  [" mch "],
    "MCHC":                 ["mchc"],
    "RDW_CV":               ["rdw"],
    "RDW_SD":               ["rdw"],
    # CBC white cell
    "TLC":                  ["tlc", "wbc", "leucocyte", "leukocyte", "total wbc"],
    "Neutrophil_Percent":   ["neutrophil", "neut"],
    "Lymphocyte_Percent":   ["lymphocyte", "lymph"],
    "Eosinophils_Percent":  ["eosinophil", "eos"],
    "Monocytes_Percent":    ["monocyte", "mono"],
    "Basophils_Percent":    ["basophil", "baso"],
    # Absolute counts
    "Neutrophils_Absolute": ["neutrophil", "neut"],
    "Lymphocytes_Absolute": ["lymphocyte", "lymph"],
    "Eosinophils_Absolute": ["eosinophil"],
    "Monocytes_Absolute":   ["monocyte"],
    "Basophils_Absolute":   ["basophil"],
    # Platelet / other CBC
    "Platelet_Count":       ["platelet", "plt"],
    "MPV":                  ["mpv"],
    "ESR":                  ["esr", "e.s.r", "erythrocyte sedimentation"],
    # Biochemistry
    "Blood_Sugar_Random":   ["blood sugar", "blood glucose", "bsr", " rbs "],
    "Serum_Creatinine":     ["creatinine", "s.creatinine"],
    "SGOT_AST":             ["sgot", "s.g.o.t", " ast "],
    "SGPT_ALT":             ["sgpt", "s.g.p.t", " alt "],
    # Urine
    "Urine_Colour":         ["colour", "color", "urine"],
    "Urine_Transparency":   ["transparency", "urine"],
    "Urine_Protein_Albumin":["protein", "albumin"],
    "Urine_Glucose":        ["glucose", "urine"],
    "Urine_Bilirubin":      ["bilirubin"],
    "Urine_Blood":          ["urine blood", "blood urine"],
    "Urine_RBC":            ["urine rbc", "rbc urine"],
    "Urine_Casts":          ["casts"],
    "Urine_Crystals":       ["crystals"],
    "Urine_PH":             ["urine ph", " ph "],
    "Urine_Specific_Gravity":["specific gravity", "sp. gravity", "sp.gr"],
}


def _prune_null_fields_for_chunk(
    null_fields: list[str],
    pages: list,
    mode: str,
) -> list[str]:
    """
    Prune null fields for a multi-page text chunk.

    For text-mode chunks: a field is included if its alias appears in ANY
    page in the chunk.  This is the union of per-page pruning — conservative
    (no false negatives) and correct for document-level extraction.

    For image-mode chunks: no pruning — visual content may not appear as
    text in OCR output, so the full null list is always sent.

    Args:
        null_fields: Fields still null after Phase 1.
        pages:       PageResult objects in this chunk.
        mode:        "text" or "image".

    Returns:
        Pruned list of null fields to send to AI.
    """
    if mode == "image":
        # Never prune image chunks — visual data may not appear in OCR text.
        return null_fields

    # Union of aliases across all pages in this text chunk.
    combined_text = " ".join(p.raw_text for p in pages if p.raw_text).lower()
    pruned: list[str] = []

    for field_name in null_fields:
        if field_name in _ALWAYS_KEEP_FIELDS:
            pruned.append(field_name)
            continue

        aliases = _FIELD_ALIASES.get(field_name)
        if not aliases:
            pruned.append(field_name)
            continue

        if any(alias in combined_text for alias in aliases):
            pruned.append(field_name)

    return pruned


# ── Data structures ────────────────────────────────────────────────────────────

@dataclass
class FileStatus:
    filename: str
    status: str = STATUS_PENDING
    patient_name: Optional[str] = None
    error_notes: Optional[str] = None
    fields_extracted: int = 0
    fields_null: int = 0
    processing_time: float = 0.0
    pages_regex_handled: int = 0
    pages_ocr_handled: int = 0
    pages_ai_handled: int = 0
    pages_graph_detected: int = 0
    unrecovered_fields: Optional[list] = field(default=None)
    total_input_tokens: int = 0
    total_output_tokens: int = 0


@dataclass
class Job:
    job_id: str
    files: list[FileStatus] = field(default_factory=list)
    status: str = STATUS_PENDING
    excel_bytes: Optional[bytes] = None
    excel_filename: Optional[str] = None


# ── In-memory job store (active processing only) ───────────────────────────────
_jobs: dict[str, Job] = {}


def create_job(filenames: list[str]) -> str:
    """Create a new job in Postgres and return its job_id."""
    job_id = str(uuid.uuid4())
    db_create_job(job_id, filenames)
    job = Job(
        job_id=job_id,
        files=[FileStatus(filename=fn) for fn in filenames],
    )
    _jobs[job_id] = job
    logger.info(f"Created job {job_id} with {len(filenames)} file(s).")
    return job_id


def get_job(job_id: str) -> Optional[Job]:
    """
    Return Job for download endpoint (needs excel_bytes).
    Checks in-memory first, then reconstructs from Postgres.
    """
    if job_id in _jobs:
        return _jobs[job_id]

    row = db_get_job(job_id)
    if not row:
        return None

    excel_bytes = db_get_excel(job_id) if row["status"] == STATUS_COMPLETE else None
    return Job(
        job_id=job_id,
        status=row["status"],
        excel_bytes=excel_bytes,
        excel_filename=row.get("excel_filename"),
    )


# ── Merge helpers ──────────────────────────────────────────────────────────────

def _empty_patient() -> dict:
    """Return a patient dict with every master field initialised to None."""
    return {col: None for col in MASTER_COLUMNS}


def _get_null_fields(patient: dict) -> list[str]:
    """Return value fields (not flag/meta) still None."""
    return [col for col in _ALL_VALUE_FIELDS if patient.get(col) is None]


def _merge_into_patient(patient: dict, source: dict) -> None:
    """
    Write values from source into patient for fields currently None.
    Never overwrites an existing value.
    Propagates paired _Flag values alongside their parent field.
    Works for both regex and AI results.
    """
    for field_name, value in source.items():
        if value is None:
            continue

        if field_name in _NARRATIVE_FIELDS:
            # Narrative fields: concatenate with pipe if a second page adds content.
            existing = patient.get(field_name)
            if existing is None:
                patient[field_name] = value
            elif str(existing).strip() != str(value).strip():
                patient[field_name] = f"{existing} | {value}"
        else:
            # All other fields: first writer wins, never overwrite.
            if patient.get(field_name) is None:
                patient[field_name] = value

        # Propagate paired _Flag value alongside its parent field.
        flag_col = f"{field_name}_Flag"
        if flag_col in FLAG_FIELDS:
            flag_val = source.get(flag_col)
            if flag_val is not None and patient.get(flag_col) is None:
                patient[flag_col] = flag_val


def _mark_graph_present(patient: dict, graph_type: Optional[str]) -> None:
    """Mark the relevant patient field PRESENT for a detected graph page."""
    mapping = {
        "ECG":              None,        # ECG has no dedicated column — ignore
        "AUDIOGRAM":        "AUDIOMETRY",
        "TMT":              "PFT",
        "SPIROMETRY_CURVE": "PFT",
        "GRAPH":            None,
    }
    col = mapping.get(graph_type or "GRAPH")
    if col and patient.get(col) is None:
        patient[col] = "PRESENT"


def _report_unrecovered_fields(
    filename: str,
    patient: dict,
    file_status: FileStatus,
    extraction_note: str,
) -> str:
    """
    After all extraction passes, identify fields still null.
    Appends a summary to extraction_note and records on file_status.
    Returns the updated extraction_note.
    """
    reportable_null = [
        f for f in _ALL_VALUE_FIELDS
        if patient.get(f) is None
        and f not in _SKIP_REPORT_FIELDS
    ]

    if not reportable_null:
        return extraction_note

    # Split into critical vs non-critical for log visibility
    _CRITICAL = {"Haemoglobin", "Blood_Group", "SGOT_AST", "SGPT_ALT", "Serum_Creatinine"}
    critical     = [f for f in reportable_null if f in _CRITICAL]
    non_critical = [f for f in reportable_null if f not in _CRITICAL]

    parts = []
    if critical:
        parts.append(f"CRITICAL_UNRECOVERED: {', '.join(critical)}")
        logger.warning(f"[{filename}] Critical fields unrecovered: {critical}")
    if non_critical:
        parts.append(f"UNRECOVERED: {', '.join(non_critical)}")
    logger.info(f"[{filename}] {len(reportable_null)} field(s) unrecovered total.")

    file_status.unrecovered_fields = reportable_null
    unrecovered_note = " | ".join(parts)
    return f"{extraction_note} | {unrecovered_note}" if extraction_note else unrecovered_note


# ── Chunk builder ──────────────────────────────────────────────────────────────

def _build_chunks(
    pending_ai: list[tuple],
    image_chunk_size: int,
    text_chunk_size: int,
) -> list[tuple[list, str]]:
    """
    Split pending_ai into chunks, grouped by mode.

    Args:
        pending_ai:        List of (PageResult, mode) from Phase 1.
        image_chunk_size:  Max pages per image-mode chunk.
        text_chunk_size:   Max pages per text-mode chunk.

    Returns:
        List of (pages_in_chunk, mode) tuples.
        Each tuple = one AI call in Phase 2.

    Strategy:
        Separate image and text pages first, then chunk each group
        independently.  This keeps image calls image-only and text calls
        text-only — extract_with_ai() takes either page_image or page_text,
        not both, so mixing modes in a chunk is not supported.
    """
    image_pages = [p for p, m in pending_ai if m == "image"]
    text_pages  = [p for p, m in pending_ai if m == "text"]

    chunks: list[tuple[list, str]] = []

    for i in range(0, len(image_pages), image_chunk_size):
        chunks.append((image_pages[i : i + image_chunk_size], "image"))

    for i in range(0, len(text_pages), text_chunk_size):
        chunks.append((text_pages[i : i + text_chunk_size], "text"))

    return chunks


# ── Single-file async pipeline ─────────────────────────────────────────────────

async def _process_single_file(
    filename: str,
    pdf_bytes: bytes,
    file_status: FileStatus,
    job_id: str,
) -> dict:
    """
    Async pipeline for one PDF.

    Phase 1 — cheap sync pass (no AI):  [UNCHANGED]
        Every page runs regex (or OCR+regex for scanned pages).
        If >= 3 fields found: page is HANDLED, AI skipped entirely.
        Graph pages: marked PRESENT, done.
        Pages not handled: queued for Phase 2.

    Phase 2 — chunked parallel AI calls:  [REFACTORED]
        pending_ai pages are grouped into chunks of _IMAGE_CHUNK_SIZE (image)
        or _TEXT_CHUNK_SIZE (text).  Each chunk fires ONE extract_with_ai()
        call.  All chunks fire concurrently via asyncio.gather — latency is
        the same as before; token cost drops by ~65% for 9-page documents.

        Old:  N pages  → N calls  → N × SYSTEM_PROMPT
        New:  N pages  → ceil(N/chunk_size) calls → ceil(N/chunk_size) × SYSTEM_PROMPT

    Returns the final validated patient record dict.
    """
    start_time = time.monotonic()
    file_status.status = STATUS_PROCESSING
    extraction_note = ""
    record: dict = {}

    patient: dict = _empty_patient()

    try:
        # ── Step 1: PDF processing (CPU-bound → thread) ────────────────────────
        pdf_result = await asyncio.to_thread(process_pdf, filename, pdf_bytes)

        if pdf_result.error:
            extraction_note = pdf_result.error
            logger.warning(f"[{filename}] PDF error: {pdf_result.error}")
            record = validate_and_clean({}, filename, extraction_note)
            file_status.status      = STATUS_FAILED
            file_status.error_notes = extraction_note
            file_status.patient_name = record.get("PatientName", filename)
            return record

        if pdf_result.partial_ocr:
            extraction_note = "PARTIAL_OCR"

        # ── Step 2: Phase 1 — regex / OCR / graph (no AI) ─────────────────────
        # UNCHANGED from original.
        # pending_ai: pages that still need AI after the free-tier pass.
        # Entries are (page, mode) where mode is "image" or "text".
        pending_ai: list[tuple] = []

        for page in pdf_result.pages:

            # ── Branch A: Graph page ───────────────────────────────────────────
            if page.is_graph_page:
                _mark_graph_present(patient, page.graph_type)
                page.handler = "GRAPH_PAGE"
                file_status.pages_graph_detected += 1
                logger.info(
                    f"[{filename}] Page {page.page_number}: GRAPH_PAGE "
                    f"({page.graph_type}) — marked PRESENT, AI skipped."
                )
                continue

            # ── Branch B: OCR page ─────────────────────────────────────────────
            if page.mode == "ocr":
                if page.ocr_text and page.ocr_above_threshold:
                    ocr_regex = extract_with_regex(page.ocr_text)
                    ocr_fields = count_regex_fields(ocr_regex)

                    if ocr_fields >= 3:
                        _merge_into_patient(patient, ocr_regex)
                        page.handler = "OCR_HANDLED"
                        file_status.pages_ocr_handled += 1
                        logger.info(
                            f"[{filename}] Page {page.page_number}: OCR_HANDLED "
                            f"(conf={page.ocr_confidence:.2f}, {ocr_fields} fields) "
                            f"— AI skipped."
                        )
                        continue

                    # OCR confidence OK but regex found < 3 fields —
                    # merge what we got and send page to AI for the rest.
                    if ocr_fields > 0:
                        _merge_into_patient(patient, ocr_regex)
                    logger.info(
                        f"[{filename}] Page {page.page_number}: OCR conf OK "
                        f"({ocr_fields} regex fields) — queued for AI."
                    )
                else:
                    if page.ocr_text and not page.ocr_above_threshold:
                        logger.info(
                            f"[{filename}] Page {page.page_number}: OCR below threshold "
                            f"(conf={page.ocr_confidence:.2f}) — queued for AI."
                        )
                    else:
                        logger.info(
                            f"[{filename}] Page {page.page_number}: No OCR text "
                            f"— queued for AI."
                        )

                if page.image_base64:
                    pending_ai.append((page, "image"))
                else:
                    logger.warning(
                        f"[{filename}] Page {page.page_number}: OCR page but no image — skipped."
                    )
                    page.handler = "SKIPPED_NO_IMAGE"
                continue

            # ── Branch C: Text page ────────────────────────────────────────────
            regex_result = extract_with_regex(page.raw_text)
            fields_found = count_regex_fields(regex_result)

            if fields_found >= 3:
                _merge_into_patient(patient, regex_result)
                page.handler = "REGEX_HANDLED"
                file_status.pages_regex_handled += 1
                logger.info(
                    f"[{filename}] Page {page.page_number}: REGEX_HANDLED "
                    f"({fields_found} fields) — AI skipped."
                )
                continue

            # Regex found < 3 fields — merge what we have, queue for AI.
            if fields_found > 0:
                _merge_into_patient(patient, regex_result)

            # Text pages always use text mode — pdfplumber text is already good
            # (≥100 chars by definition). Sending image costs ~800 vision tokens
            # for no accuracy gain. Image mode is only for OCR pages (Branch B).
            if page.raw_text:
                pending_ai.append((page, "text"))
            else:
                page.handler = "SKIPPED_NO_NULLS"

        # ── Step 3: Phase 2 — chunked AI calls ────────────────────────────────
        #
        # null_fields_snapshot is taken HERE — after ALL Phase 1 merges — so
        # every chunk call targets exactly the fields still null, with no
        # redundancy.  This is more aggressive pruning than the old per-page
        # snapshot because Phase 1 may have filled more fields across pages
        # before any AI call fires.
        #
        # CHANGED: asyncio.gather now runs one coroutine per CHUNK, not one
        # per page.  _call_page() replaced by _call_chunk().

        null_fields_snapshot = _get_null_fields(patient)

        # Drop pending pages if nothing is left to extract.
        pending_ai = [
            (page, mode) for page, mode in pending_ai
            if null_fields_snapshot
        ]

        if pending_ai:
            # Build chunks — this is where N pages become ceil(N/chunk_size) calls.
            chunks = _build_chunks(pending_ai, _IMAGE_CHUNK_SIZE, _TEXT_CHUNK_SIZE)

            n_image_chunks = sum(1 for _, m in chunks if m == "image")
            n_text_chunks  = sum(1 for _, m in chunks if m == "text")
            logger.info(
                f"[{filename}] Phase 2: {len(pending_ai)} pending page(s) → "
                f"{len(chunks)} AI call(s) "
                f"({n_image_chunks} image chunk(s), {n_text_chunks} text chunk(s)). "
                f"Null fields: {len(null_fields_snapshot)}."
            )

            # ── _call_chunk: one AI call per chunk ────────────────────────────
            async def _call_chunk(pages: list, mode: str) -> tuple:
                """
                Execute one AI extraction call for a group of pages.

                Image mode:
                    Passes all page images as a list — extract_with_ai sends
                    them as multiple image_url entries in the vision payload.
                    Full null_fields_snapshot used (no pruning for image chunks).

                Text mode:
                    Combines page texts with PAGE BREAK delimiters into a
                    single text block.  Pruning is applied across the union
                    of all pages in the chunk — a field is kept if its alias
                    appears in any page in the chunk.

                Returns:
                    (pages, ai_result, ai_note, inp_tok, out_tok)
                    pages is the input list — returned so the caller can update
                    page.handler and log per-page stats.
                """
                if mode == "image":
                    chunk_null_fields = null_fields_snapshot   # no pruning for images

                    # Collect base64 images from all pages in this chunk.
                    images = [p.image_base64 for p in pages if p.image_base64]

                    if not images:
                        logger.warning(
                            f"[{filename}] Image chunk has no renderable pages — skipped."
                        )
                        for p in pages:
                            p.handler = "SKIPPED_NO_IMAGE"
                        return pages, {}, "", 0, 0

                    page_nums = [p.page_number for p in pages]
                    logger.info(
                        f"[{filename}] Image chunk pages {page_nums}: "
                        f"{len(images)} image(s), {len(chunk_null_fields)} null fields."
                    )

                    ai_result, ai_note, inp_tok, out_tok = await extract_with_ai(
                        null_fields=chunk_null_fields,
                        page_image=images[0] if len(images) == 1 else None,
                        page_images=images if len(images) > 1 else None,
                        mode="image",
                    )

                else:
                    # Text mode: prune across union of all pages in chunk.
                    chunk_null_fields = _prune_null_fields_for_chunk(
                        null_fields_snapshot, pages, mode="text"
                    )

                    if not chunk_null_fields:
                        logger.info(
                            f"[{filename}] Text chunk pages "
                            f"{[p.page_number for p in pages]}: "
                            f"pruned to 0 null fields — AI call skipped."
                        )
                        for p in pages:
                            p.handler = "SKIPPED_NO_NULLS"
                        return pages, {}, "", 0, 0

                    # Combine page texts with clear delimiters.
                    combined_text = "\n\n---PAGE BREAK---\n\n".join(
                        f"[PAGE {p.page_number}]\n{p.raw_text}"
                        for p in pages
                        if p.raw_text
                    )

                    page_nums = [p.page_number for p in pages]
                    logger.info(
                        f"[{filename}] Text chunk pages {page_nums}: "
                        f"{len(chunk_null_fields)} null fields, "
                        f"{len(combined_text)} chars combined."
                    )

                    ai_result, ai_note, inp_tok, out_tok = await extract_with_ai(
                        null_fields=chunk_null_fields,
                        page_text=combined_text,
                        mode="text",
                    )

                return pages, ai_result, ai_note, inp_tok, out_tok

            # Fire all chunks concurrently — same parallelism as old per-page gather.
            # REPLACED: asyncio.gather over pages → asyncio.gather over chunks.
            chunk_results = await asyncio.gather(
                *[_call_chunk(pages, mode) for pages, mode in chunks],
                return_exceptions=True,
            )

            # ── Step 4: Merge chunk AI results (sequential — no I/O) ──────────
            for outcome in chunk_results:
                if isinstance(outcome, Exception):
                    logger.error(f"[{filename}] AI chunk raised: {outcome}")
                    extraction_note = (
                        f"{extraction_note} | API_ERROR" if extraction_note else "API_ERROR"
                    )
                    continue

                pages, ai_result, ai_note, inp_tok, out_tok = outcome

                # Cost log: one DB row per chunk.
                # page_number is None for multi-page chunks (no single page to attribute).
                page_nums_str = ",".join(str(p.page_number) for p in pages)
                chunk_page_number = pages[0].page_number if len(pages) == 1 else None

                await asyncio.to_thread(
                    db_insert_openai_call,
                    job_id, filename, chunk_page_number, "chunked",
                    inp_tok, out_tok,
                    _estimate_cost(inp_tok, out_tok),
                    bool(ai_result and not ai_note),
                    ai_note or None,
                )

                if ai_note:
                    extraction_note = (
                        f"{extraction_note} | {ai_note}" if extraction_note else ai_note
                    )

                file_status.total_input_tokens  += inp_tok
                file_status.total_output_tokens += out_tok

                if ai_result:
                    _merge_into_patient(patient, ai_result)
                    # Increment pages_ai_handled once per page in this chunk.
                    file_status.pages_ai_handled += len(pages)
                    logger.info(
                        f"[{filename}] Chunk pages [{page_nums_str}]: AI_HANDLED "
                        f"— {len(pages)} page(s) in chunk, "
                        f"inp={inp_tok} out={out_tok} tokens."
                    )

                # Mark all pages in this chunk as AI_HANDLED.
                for page in pages:
                    page.handler = "AI_HANDLED"

        # ── Step 5: Report unrecovered fields ──────────────────────────────────
        extraction_note = _report_unrecovered_fields(
            filename=filename,
            patient=patient,
            file_status=file_status,
            extraction_note=extraction_note,
        )

        # ── Step 6: Validate, clean, and run plausibility checks ──────────────
        record = validate_and_clean(patient, filename, extraction_note)

    except Exception as exc:
        logger.error(f"[{filename}] Unexpected pipeline error: {exc}", exc_info=True)
        extraction_note = "API_ERROR"
        record = validate_and_clean({}, filename, extraction_note)
        file_status.status      = STATUS_FAILED
        file_status.error_notes = extraction_note

    finally:
        elapsed = time.monotonic() - start_time
        file_status.processing_time = round(elapsed, 2)

    # ── Finalise file_status ───────────────────────────────────────────────────
    if file_status.status != STATUS_FAILED:
        note = record.get("Extraction_Note") or ""
        has_error = any(
            err in note for err in (
                "API_ERROR", "PDF_CORRUPTED", "PDF_PASSWORD_PROTECTED",
            )
        )
        file_status.status      = STATUS_FAILED if has_error else STATUS_DONE
        file_status.error_notes = note if note else None

    file_status.patient_name = record.get("PatientName")
    extracted, null_count    = count_fields(record)
    file_status.fields_extracted = extracted
    file_status.fields_null      = null_count

    return record


# ── Batch runner ───────────────────────────────────────────────────────────────

async def run_batch(job_id: str, files: list[tuple[str, bytes]]) -> None:
    """
    Process all PDFs for a job concurrently (max MAX_CONCURRENT_EXTRACTIONS).
    Builds and stores the Excel output when all files are done.
    One file failing never stops the others.
    """
    job = _jobs.get(job_id)
    if not job:
        logger.error(f"Job {job_id} not found in store.")
        return

    job.status = STATUS_PROCESSING
    await asyncio.to_thread(db_set_job_status, job_id, STATUS_PROCESSING)

    semaphore = asyncio.Semaphore(MAX_CONCURRENT_EXTRACTIONS)
    records: list[Optional[dict]] = [None] * len(files)

    async def process_one(index: int, filename: str, pdf_bytes: bytes) -> None:
        file_status = job.files[index]
        async with semaphore:
            try:
                record = await _process_single_file(
                    filename, pdf_bytes, file_status, job_id
                )
                records[index] = record
                await asyncio.to_thread(db_update_file, job_id, filename, file_status)
            except Exception as exc:
                logger.error(f"[{filename}] Unhandled error: {exc}", exc_info=True)
                extraction_note = "API_ERROR"
                record = validate_and_clean({}, filename, extraction_note)
                records[index]          = record
                file_status.status      = STATUS_FAILED
                file_status.error_notes = extraction_note
                file_status.patient_name = record.get("PatientName", filename)
                await asyncio.to_thread(db_update_file, job_id, filename, file_status)

    tasks = [
        asyncio.create_task(process_one(i, fn, pb))
        for i, (fn, pb) in enumerate(files)
    ]
    await asyncio.gather(*tasks, return_exceptions=True)

    # ── Build Excel ────────────────────────────────────────────────────────────
    summaries = []
    for fs in job.files:
        summaries.append({
            "Filename":                fs.filename,
            "PatientName":             fs.patient_name,
            "Status":                  fs.status,
            "Fields_Extracted":        fs.fields_extracted,
            "Fields_Null":             fs.fields_null,
            "Processing_Time_Seconds": fs.processing_time,
            "Error_Notes":             fs.error_notes,
            "Pages_Regex_Handled":     fs.pages_regex_handled,
            "Pages_OCR_Handled":       fs.pages_ocr_handled,
            "Pages_AI_Handled":        fs.pages_ai_handled,
            "Pages_Graph_Detected":    fs.pages_graph_detected,
            "Unrecovered_Fields":      ", ".join(fs.unrecovered_fields) if fs.unrecovered_fields else None,
        })

    final_records = [r for r in records if r is not None]

    try:
        excel_bytes = await asyncio.to_thread(
            build_excel, final_records, summaries
        )
        job.excel_bytes    = excel_bytes
        job.excel_filename = get_output_filename()
        await asyncio.to_thread(
            db_save_excel, job_id, excel_bytes, job.excel_filename
        )
        logger.info(f"Job {job_id} — Excel built: {job.excel_filename}")
    except Exception as exc:
        logger.error(f"Job {job_id} — Excel build failed: {exc}", exc_info=True)

    job.status = STATUS_COMPLETE
    await asyncio.to_thread(db_set_job_status, job_id, STATUS_COMPLETE)
    _jobs.pop(job_id, None)
    logger.info(f"Job {job_id} complete. {len(final_records)} record(s) processed.")


# ── Status snapshot ────────────────────────────────────────────────────────────

def get_job_status_payload(job_id: str) -> Optional[dict]:
    """
    Build the JSON-serializable status payload for GET /status/{job_id}.
    Reads live from in-memory state during processing, Postgres after completion.
    """
    if job_id in _jobs:
        job = _jobs[job_id]
        total       = len(job.files)
        completed   = sum(1 for f in job.files if f.status == STATUS_DONE)
        failed      = sum(1 for f in job.files if f.status == STATUS_FAILED)
        in_progress = sum(1 for f in job.files if f.status == STATUS_PROCESSING)
        return {
            "job_id":      job_id,
            "total":       total,
            "completed":   completed,
            "failed":      failed,
            "in_progress": in_progress,
            "status":      job.status,
            "files": [
                {
                    "filename":     f.filename,
                    "status":       f.status,
                    "patient_name": f.patient_name,
                }
                for f in job.files
            ],
        }

    job_row = db_get_job(job_id)
    if not job_row:
        return None

    file_rows   = db_get_files(job_id)
    total       = len(file_rows)
    completed   = sum(1 for f in file_rows if f["status"] == STATUS_DONE)
    failed      = sum(1 for f in file_rows if f["status"] == STATUS_FAILED)
    in_progress = sum(1 for f in file_rows if f["status"] == STATUS_PROCESSING)

    return {
        "job_id":      job_id,
        "total":       total,
        "completed":   completed,
        "failed":      failed,
        "in_progress": in_progress,
        "status":      job_row["status"],
        "files": [
            {
                "filename":     f["filename"],
                "status":       f["status"],
                "patient_name": f["patient_name"],
            }
            for f in file_rows
        ],
    }
