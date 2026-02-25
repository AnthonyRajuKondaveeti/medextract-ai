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


def _prune_null_fields_for_page(null_fields: list[str], page_text: str) -> list[str]:
    """
    For a given page's raw text, return only those null fields whose aliases
    appear somewhere in the text.  Fields in _ALWAYS_KEEP_FIELDS are retained
    unconditionally (visual/narrative content may not appear as text).

    Called only for text-mode pages.  OCR pages are sent with the full null
    list because low-confidence OCR text may have missed keywords even if the
    data is present in the image.

    Returns the pruned list.  If the result is empty the caller skips the AI
    call entirely — guaranteed zero tokens wasted on that page.
    """
    text_lower = page_text.lower()
    pruned: list[str] = []

    for field_name in null_fields:
        # Always keep fields that can't be reliably detected from text alone
        if field_name in _ALWAYS_KEEP_FIELDS:
            pruned.append(field_name)
            continue

        aliases = _FIELD_ALIASES.get(field_name)
        if not aliases:
            # No alias defined — keep it to be safe (no false negatives)
            pruned.append(field_name)
            continue

        if any(alias in text_lower for alias in aliases):
            pruned.append(field_name)
        # else: no alias found on this page — field cannot be here, skip it

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


# ── Single-file async pipeline ─────────────────────────────────────────────────

async def _process_single_file(
    filename: str,
    pdf_bytes: bytes,
    file_status: FileStatus,
    job_id: str,
) -> dict:
    """
    Async pipeline for one PDF.

    Phase 1 — cheap sync pass (no AI):
        Every page runs regex (or OCR+regex for scanned pages).
        If >= 3 fields found: page is HANDLED, AI skipped entirely.
        Graph pages: marked PRESENT, done.
        Pages not handled: queued for Phase 2.

    Phase 2 — parallel AI calls:
        All queued pages fire concurrently via asyncio.gather.
        Each call receives only the null fields not yet filled by Phase 1.
        AI_CONCURRENCY semaphore (in ai_extractor) caps global concurrency.

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

        # ── Step 2: Phase 1 — regex / OCR / graph (no AI) ────────────────────
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

            if page.image_base64:
                pending_ai.append((page, "image"))
            elif page.raw_text:
                pending_ai.append((page, "text"))
            else:
                page.handler = "SKIPPED_NO_NULLS"

        # ── Step 3: Phase 2 — snapshot null fields, fire AI in parallel ───────
        # Snapshot taken after ALL Phase 1 merges so every AI call targets
        # exactly the fields still null — no redundancy, minimal token spend.
        null_fields_snapshot = _get_null_fields(patient)

        # Prune pages where regex already filled everything
        pending_ai = [
            (page, mode) for page, mode in pending_ai
            if null_fields_snapshot
        ]

        if pending_ai:
            logger.info(
                f"[{filename}] Phase 2: {len(pending_ai)} AI call(s) in parallel "
                f"({len(null_fields_snapshot)} null fields each)."
            )

            async def _call_page(page, mode: str) -> tuple:
                if mode == "image":
                    # OCR / scanned pages: use full null list.
                    # Low-confidence OCR text may have missed keywords even
                    # when the data is present visually — no pruning here.
                    page_null_fields = null_fields_snapshot
                else:
                    # Text pages: prune to only fields whose aliases appear
                    # in the page text. Eliminates asking AI for fields that
                    # are structurally absent from this page.
                    page_null_fields = _prune_null_fields_for_page(
                        null_fields_snapshot, page.raw_text
                    )

                if not page_null_fields:
                    # Nothing left to ask — skip AI call entirely.
                    logger.info(
                        f"[{filename}] Page {page.page_number}: "
                        f"pruned null fields to 0 — AI call skipped."
                    )
                    page.handler = "SKIPPED_NO_NULLS"
                    return page, {}, "", 0, 0

                if mode == "image":
                    ai_result, ai_note, inp_tok, out_tok = await extract_with_ai(
                        null_fields=page_null_fields,
                        page_image=page.image_base64,
                        mode="image",
                    )
                else:
                    ai_result, ai_note, inp_tok, out_tok = await extract_with_ai(
                        null_fields=page_null_fields,
                        page_text=page.raw_text,
                        mode="text",
                    )
                return page, ai_result, ai_note, inp_tok, out_tok

            results = await asyncio.gather(
                *[_call_page(page, mode) for page, mode in pending_ai],
                return_exceptions=True,
            )

            # ── Step 4: Merge AI results (sequential — no I/O) ────────────────
            for outcome in results:
                if isinstance(outcome, Exception):
                    logger.error(f"[{filename}] AI task raised: {outcome}")
                    extraction_note = (
                        f"{extraction_note} | API_ERROR" if extraction_note else "API_ERROR"
                    )
                    continue

                page, ai_result, ai_note, inp_tok, out_tok = outcome

                await asyncio.to_thread(
                    db_insert_openai_call,
                    job_id, filename, page.page_number, "per_page",
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
                    file_status.pages_ai_handled += 1
                    logger.info(
                        f"[{filename}] Page {page.page_number}: AI_HANDLED "
                        f"({page.mode}/{['text','image'][page.image_base64 is not None]}) "
                        f"— fields requested / snapshot: "
                        f"{len(null_fields_snapshot)} total, see pruning log above."
                    )

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