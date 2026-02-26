"""
ai_extractor.py

Async OpenAI extraction layer.

Pipeline position:
    regex → OCR → AI  (this module handles the AI step only)

All calls are async — never blocks the event loop waiting on network I/O.
Concurrency is capped by a module-level asyncio.Semaphore (AI_CONCURRENCY).

Public API:
    extract_with_ai(null_fields, page_image, page_text, mode) → async
        Returns (partial_dict, extraction_note, input_tokens, output_tokens).

Cost note:
    Regex handles ~70% of structured fields for free.
    Local OCR handles scanned pages without AI where confidence permits.
    AI only processes null fields per page image — no full-document calls ever.
    Estimated token reduction: 65-80% per report vs naive full-document approach.
"""

import asyncio
import json
import logging
import random
from typing import Optional

import httpx

from config import AI_CONCURRENCY, AI_MAX_RETRIES, MOCK_AI, MODEL, OPENAI_API_KEY

logger = logging.getLogger(__name__)

# ── Concurrency limiter ────────────────────────────────────────────────────────
# Lazy-initialised so it is created inside a running event loop.
# asyncio.Semaphore() must not be instantiated at module import time —
# Python 3.10+ raises DeprecationWarning and some environments hard-fail.
# _get_semaphore() is called at the first AI request, by which point
# FastAPI's event loop is always running.
_ai_semaphore: Optional[asyncio.Semaphore] = None


def _get_semaphore() -> asyncio.Semaphore:
    global _ai_semaphore
    if _ai_semaphore is None:
        _ai_semaphore = asyncio.Semaphore(AI_CONCURRENCY)
    return _ai_semaphore

# ── Persistent async HTTP client ───────────────────────────────────────────────
# Single client reused for all calls — avoids TCP handshake overhead per request.
# Timeout: 60s connect, 120s read (vision calls with large images can be slow).
_http_client: Optional[httpx.AsyncClient] = None


def _get_http_client() -> httpx.AsyncClient:
    global _http_client
    if _http_client is None or _http_client.is_closed:
        _http_client = httpx.AsyncClient(
            base_url="https://api.openai.com",
            headers={
                "Authorization": f"Bearer {OPENAI_API_KEY}",
                "Content-Type": "application/json",
            },
            timeout=httpx.Timeout(connect=60.0, read=120.0, write=30.0, pool=10.0),
            limits=httpx.Limits(max_connections=50, max_keepalive_connections=20),
        )
    return _http_client


# ── Prompts ────────────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """You are a medical report data extractor.
You extract structured data from Indian lab reports.
Labs across India use different formats, layouts,
column orders, and terminology.
You normalize everything to a standard output.
You always return valid JSON. Nothing else.
No explanation. No markdown. No backticks.
Just the raw JSON object.

FIELD VARIATION MAPPINGS:
Normalize all of these to the standard column name:

Haemoglobin:
  "Hb", "HB%", "Haemoglobin (HB%)", "HAEMOGLOBIN",
  "Hemoglobin", "HAEMOGLOBIN (HB%)"

TLC:
  "Total WBC", "Total Leucocyte Count",
  "Total W.B.C", "TOTAL WBC COUNT", "WBC",
  "Total W.B.C Count"

Blood_Sugar_Random:
  "BSR", "RBS", "Blood Glucose Random",
  "BLOOD GLUCOSE RANDOM", "Random Blood Sugar",
  "Blood Sugar Random"

Serum_Creatinine:
  "S.Creatinine", "CREATININE", "Creatinine",
  "S. Creatinine"

SGOT_AST:
  "SGOT", "AST", "S.G.O.T", "SGOT/AST",
  "SGOT, AST", "SGOT (SERUM)", "SGOT/AST (Aspartate Transaminase)"

SGPT_ALT:
  "SGPT", "ALT", "S.G.P.T", "SGPT/ALT",
  "SGPT, ALT", "SGPT (SERUM)", "SGPT/ALT (Alanine Transaminase)"

Hct:
  "PCV", "HCT", "Haematocrit", "P.C.V"

Red_Blood_Cell_Count:
  "RBC", "RBC Count", "R.B.C", "R.B.C Count",
  "RBC COUNT", "Red Blood Cell"

Platelet_Count:
  "PLT", "Platelets", "Platelet Count",
  "PLATELET COUNT", "Plt Count"

Neutrophil_Percent:
  "Neutrophils", "NEUTROPHILS", "Neutrophil %",
  "Neut%", "Neutrophil"

Lymphocyte_Percent:
  "Lymphocytes", "LYMPHOCYTES", "Lymphocyte %",
  "Lymph%"

Eosinophils_Percent:
  "Eosinophils", "EOSINOPHILS", "Eosinophil %",
  "Eos%"

Monocytes_Percent:
  "Monocytes", "MONOCYTES", "Monocyte %"

Basophils_Percent:
  "Basophils", "BASOPHILS", "Basophil %"

ESR:
  "E.S.R", "ESR (Westergren)",
  "Erythrocyte Sedimentation Rate",
  "ESR*", "ERYTHROCYTE SEDIMENTATION RATE(ESR)"

Blood_Group:
  "Blood Group", "ABO Group", "BLOOD GROUP",
  "Blood Group (ABO)"

XRAY:
  "X-Ray", "Chest PA View", "CXR",
  "CHEST PA VIEW", "X-RAY"
  -> Extract the IMPRESSION text only

PFT:
  "Spirometry", "Pulmonary Function", "PFT",
  "Spirometry(FVC Results)"
  -> Extract the interpretation/conclusion text

AUDIOMETRY:
  "Audiometry", "Audiological Evaluation",
  "Hearing Test"
  -> If graph only -> return "PRESENT"
  -> If diagnosis text exists -> return that text

FLAG DETECTION:
Detect abnormal flags from ANY format and extract as SEPARATE fields:
- "12.6 Low"    -> "Haemoglobin": "12.6", "Haemoglobin_Flag": "LOW"
- "12.6 L"      -> "Haemoglobin": "12.6", "Haemoglobin_Flag": "LOW"
- "12.6 H"      -> "Haemoglobin": "12.6", "Haemoglobin_Flag": "HIGH"
- "12.6 High"   -> "Haemoglobin": "12.6", "Haemoglobin_Flag": "HIGH"
- "up arrow" symbol    -> store in field_Flag: "HIGH"
- "down arrow" symbol  -> store in field_Flag: "LOW"
- Bold value outside reference range -> flag accordingly
- Standalone "L" or "H" on the line immediately after
  a test value -> that flag belongs to the test above it
- Always separate value from flag in output using separate keys
- NEVER return nested objects like {"value": X, "flag": Y}
- ALWAYS use flat structure: field: value, field_Flag: flag
- If value is within reference range and no flag printed
  -> field_Flag: null"""

_FOCUSED_PROMPT = """Extract only the following specific fields from {scope}.
Return a JSON object containing ONLY these fields.
Set a field to null if it is not present anywhere in {scope}.
Do not guess. Do not hallucinate values.
No explanation. No markdown. Just the raw JSON object.

Fields to extract:
{null_fields_list}

FLAG DETECTION:
When you see flagged values like "12.6 L" or "130/85 High", extract them as SEPARATE fields:
  "Haemoglobin": "12.6",
  "Haemoglobin_Flag": "LOW"

NOT like this (WRONG):
  "Haemoglobin": {{"value": "12.6", "flag": "LOW"}}

Always use flat JSON structure with separate keys for flags.
For "12.6 L" or "12.6 Low" -> extract as field: "12.6", field_Flag: "LOW"
For "12.6 H" or "12.6 High" -> extract as field: "12.6", field_Flag: "HIGH"
Standalone H/L on next line -> store in the _Flag field for that test.

{content_block}"""


def _build_null_fields_str(null_fields: list[str]) -> str:
    return "\n".join(f"  - {f}" for f in null_fields) if null_fields else "  (all fields)"


# ── Core async OpenAI call ─────────────────────────────────────────────────────

async def _call_openai_async(
    user_prompt: str,
    images: Optional[list[str]] = None,
) -> tuple[str, int, int]:
    """
    Make one async POST to OpenAI /v1/chat/completions.
    Returns (response_text, input_tokens, output_tokens).
    Raises httpx.HTTPStatusError or httpx.RequestError on failure.
    """
    content: list = []

    if images:
        for b64_image in images:
            content.append({
                "type": "image_url",
                "image_url": {
                    "url": f"data:image/png;base64,{b64_image}",
                    "detail": "high",
                },
            })

    content.append({"type": "text", "text": user_prompt})

    payload = {
        "model": MODEL,
        "max_tokens": 4096,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user",   "content": content},
        ],
    }

    client = _get_http_client()
    response = await client.post("/v1/chat/completions", json=payload)
    response.raise_for_status()

    data = response.json()
    text         = data["choices"][0]["message"]["content"]
    input_tokens  = data["usage"]["prompt_tokens"]
    output_tokens = data["usage"]["completion_tokens"]
    return text, input_tokens, output_tokens


# ── Retry wrapper ──────────────────────────────────────────────────────────────

async def _call_ai_with_retry(
    user_prompt: str,
    images: Optional[list[str]] = None,
) -> tuple[dict, str, int, int]:
    """
    Async retry wrapper around _call_openai_async.
    Returns (data_dict, error_note, input_tokens, output_tokens).
    error_note is "" on success, "API_ERROR" on terminal failure.
    Tokens are accumulated across retry attempts.
    """
    total_input  = 0
    total_output = 0

    for attempt in range(AI_MAX_RETRIES + 1):
        try:
            async with _get_semaphore():
                raw, inp, out = await _call_openai_async(user_prompt, images)

            total_input  += inp
            total_output += out

            # Strip accidental markdown fences from response
            cleaned = raw.strip()
            if cleaned.startswith("```"):
                lines   = [l for l in cleaned.split("\n") if not l.strip().startswith("```")]
                cleaned = "\n".join(lines).strip()

            data = json.loads(cleaned)
            if not isinstance(data, dict):
                raise ValueError("AI response is not a JSON object")

            return data, "", total_input, total_output

        except (json.JSONDecodeError, ValueError) as exc:
            logger.warning(
                f"AI returned invalid JSON (attempt {attempt + 1}/{AI_MAX_RETRIES + 1}): {exc}"
            )
            if attempt < AI_MAX_RETRIES:
                continue
            return {}, "API_ERROR", total_input, total_output

        except httpx.HTTPStatusError as exc:
            logger.error(
                f"OpenAI HTTP error {exc.response.status_code} "
                f"(attempt {attempt + 1}/{AI_MAX_RETRIES + 1}): {exc}"
            )
            if attempt < AI_MAX_RETRIES:
                # Exponential backoff: 1s, 2s, 4s, 8s, 16s + jitter
                backoff = (2 ** attempt) + random.uniform(0, 1)
                logger.info(f"Retrying in {backoff:.1f}s...")
                await asyncio.sleep(backoff)
                continue
            return {}, "API_ERROR", total_input, total_output

        except Exception as exc:
            logger.error(
                f"AI call failed (attempt {attempt + 1}/{AI_MAX_RETRIES + 1}): {exc}"
            )
            if attempt < AI_MAX_RETRIES:
                # Exponential backoff: 1s, 2s, 4s, 8s, 16s + jitter
                backoff = (2 ** attempt) + random.uniform(0, 1)
                logger.info(f"Retrying in {backoff:.1f}s...")
                await asyncio.sleep(backoff)
                continue
            return {}, "API_ERROR", total_input, total_output

    return {}, "API_ERROR", total_input, total_output


# ── Public entry point ─────────────────────────────────────────────────────────

async def extract_with_ai(
    null_fields: list[str],
    page_image: Optional[str] = None,
    page_images: Optional[list[str]] = None,
    page_text: Optional[str] = None,
    mode: str = "image",
) -> tuple[dict, str, int, int]:
    """
    Async AI extraction — supports single-page and multi-page (chunked) calls.

    Args:
        null_fields:  Fields not yet filled by regex/OCR — only these are requested.
        page_image:   Base64 PNG of a single page (single-page image call).
        page_images:  List of base64 PNGs for a multi-page image chunk.
                      When provided, takes precedence over page_image.
                      _call_openai_async already supports list[str] images natively.
        page_text:    Raw text of the page(s) (used when mode="text").
                      For chunked text calls, pass pre-combined text with
                      ---PAGE BREAK--- delimiters.
        mode:         "image" (vision call) or "text" (text-only call).

    Returns:
        (partial_dict, extraction_note, input_tokens, output_tokens)
        extraction_note is "LLM_MOCK" when MOCK_AI=true, "API_ERROR" on failure, "" on success.
    """
    if not null_fields:
        return {}, "", 0, 0

    if MOCK_AI:
        logger.info("MOCK_AI enabled — skipping OpenAI call.")
        return {}, "LLM_MOCK", 0, 0

    null_fields_list = _build_null_fields_str(null_fields)

    if mode == "image":
        # Resolve image list: page_images (chunk) takes precedence over page_image (single).
        images: Optional[list[str]] = None
        if page_images:
            images = page_images
        elif page_image:
            images = [page_image]

        if images:
            n = len(images)
            if n == 1:
                scope         = "this medical report page"
                content_block = "PAGE IMAGE: (attached above)"
            else:
                scope         = f"these {n} medical report pages"
                content_block = f"{n} PAGE IMAGES: (attached above, in page order)"
            user_prompt = _FOCUSED_PROMPT.format(
                scope=scope,
                null_fields_list=null_fields_list,
                content_block=content_block,
            )
            data, note, inp, out = await _call_ai_with_retry(user_prompt, images=images)
            return data, note, inp, out

    # Text mode — page_text contains either a single page or pre-combined
    # multi-page text with ---PAGE BREAK--- delimiters (from batch_processor).
    page_break_count = (page_text or "").count("---PAGE BREAK---")
    if page_break_count == 0:
        scope = "this medical report page"
    else:
        scope = f"these {page_break_count + 1} medical report pages"
    content_block = f"PAGE TEXT:\n{page_text or ''}"
    user_prompt = _FOCUSED_PROMPT.format(
        scope=scope,
        null_fields_list=null_fields_list,
        content_block=content_block,
    )
    data, note, inp, out = await _call_ai_with_retry(user_prompt)
    return data, note, inp, out