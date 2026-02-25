"""
regex_extractor.py

Layer 1 of the page-level hybrid extraction pipeline.
Called once per PDF page, not once per PDF.

Decision rule used by batch_processor:
  count_regex_fields(result) >= 3  → REGEX_HANDLED  (skip Claude for this page)
  is_graph_page (in pdf_processor) → GRAPH_PAGE     (skip Claude for this page)
  count_regex_fields(result) < 3   → CLAUDE_HANDLED (send page image to Claude)

Deterministic — cannot hallucinate. Either finds a pattern or returns None.
"""

import re
from typing import Optional

# ── Helpers ────────────────────────────────────────────────────────────────────

_NUMBER = r"(\d+(?:\.\d+)?)"
_FLAG_PATTERN = re.compile(
    r"(?:^|\s)(H|L|High|Low|↑|↓)\s*$",
    re.IGNORECASE | re.MULTILINE,
)
_INLINE_FLAG = re.compile(
    r"\b(\d+(?:\.\d+)?)\s+(H|L|High|Low|↑|↓)\b",
    re.IGNORECASE,
)


def _normalize_flag(raw: str) -> Optional[str]:
    if not raw:
        return None
    u = raw.strip().upper()
    if u in ("H", "HIGH", "↑"):
        return "HIGH"
    if u in ("L", "LOW", "↓"):
        return "LOW"
    return None


def _extract_value_and_flag(
    text: str,
    pattern: re.Pattern,
    field_name: str = "",
) -> tuple[Optional[float], Optional[str]]:
    """
    Search for pattern in text.
    Returns (numeric_value, flag) or (None, None) on no match.
    Checks the matched line and the next line for flag tokens.
    """
    match = pattern.search(text)
    if not match:
        return None, None

    raw_val = match.group(1)
    try:
        value = float(raw_val)
    except ValueError:
        return None, None

    # Check for inline flag: "12.6 L" or "12.6 High"
    # Look at the full matched line
    line_start = text.rfind("\n", 0, match.start()) + 1
    line_end   = text.find("\n", match.end())
    if line_end == -1:
        line_end = len(text)
    matched_line = text[line_start:line_end]

    inline = _INLINE_FLAG.search(matched_line)
    if inline:
        flag = _normalize_flag(inline.group(2))
        return value, flag

    # Check next line for standalone flag
    next_line_end = text.find("\n", line_end + 1)
    if next_line_end == -1:
        next_line_end = len(text)
    next_line = text[line_end:next_line_end].strip()
    standalone = re.fullmatch(r"(H|L|High|Low|↑|↓)", next_line, re.IGNORECASE)
    if standalone:
        return value, _normalize_flag(standalone.group(1))

    return value, None


# ── Field patterns ─────────────────────────────────────────────────────────────
# Each entry: (compiled_pattern, field_name)
# Pattern captures the numeric value in group 1.

_NUMERIC_PATTERNS: list[tuple[re.Pattern, str]] = [
    # Haemoglobin
    (re.compile(
        r"(?:Haemoglobin|Hemoglobin|HAEMOGLOBIN|HB%?|Hb)\s*[:\-]?\s*" + _NUMBER,
        re.IGNORECASE), "Haemoglobin"),

    # Red Blood Cell Count
    (re.compile(
        r"(?:RBC(?:\s+Count)?|R\.?B\.?C\.?(?:\s+Count)?|Red\s+Blood\s+Cell(?:\s+Count)?)\s*[:\-]?\s*" + _NUMBER,
        re.IGNORECASE), "Red_Blood_Cell_Count"),

    # Hct / PCV
    (re.compile(
        r"(?:Haematocrit|Hematocrit|HCT|PCV|P\.?C\.?V\.?)\s*[:\-]?\s*" + _NUMBER,
        re.IGNORECASE), "Hct"),

    # MCV
    (re.compile(r"MCV\s*[:\-]?\s*" + _NUMBER, re.IGNORECASE), "MCV"),

    # MCH
    (re.compile(r"\bMCH\b\s*[:\-]?\s*" + _NUMBER, re.IGNORECASE), "MCH"),

    # MCHC
    (re.compile(r"MCHC\s*[:\-]?\s*" + _NUMBER, re.IGNORECASE), "MCHC"),

    # RDW-CV
    (re.compile(r"RDW[-\s]?CV\s*[:\-]?\s*" + _NUMBER, re.IGNORECASE), "RDW_CV"),

    # RDW-SD
    (re.compile(r"RDW[-\s]?SD\s*[:\-]?\s*" + _NUMBER, re.IGNORECASE), "RDW_SD"),

    # TLC / WBC
    (re.compile(
        r"(?:Total\s+(?:Leucocyte|Leukocyte|WBC)\s+Count|TLC|Total\s+W\.?B\.?C\.?(?:\s+Count)?|TOTAL\s+WBC(?:\s+COUNT)?)\s*[:\-]?\s*" + _NUMBER,
        re.IGNORECASE), "TLC"),

    # Neutrophil %
    (re.compile(
        r"(?:Neutrophils?|NEUTROPHILS?|Neut%?)\s*[:\-]?\s*" + _NUMBER,
        re.IGNORECASE), "Neutrophil_Percent"),

    # Lymphocyte %
    (re.compile(
        r"(?:Lymphocytes?|LYMPHOCYTES?|Lymph%?)\s*[:\-]?\s*" + _NUMBER,
        re.IGNORECASE), "Lymphocyte_Percent"),

    # Eosinophils %
    (re.compile(
        r"(?:Eosinophils?|EOSINOPHILS?|Eos%?)\s*[:\-]?\s*" + _NUMBER,
        re.IGNORECASE), "Eosinophils_Percent"),

    # Monocytes %
    (re.compile(
        r"(?:Monocytes?|MONOCYTES?|Mono%?)\s*[:\-]?\s*" + _NUMBER,
        re.IGNORECASE), "Monocytes_Percent"),

    # Basophils %
    (re.compile(
        r"(?:Basophils?|BASOPHILS?|Baso%?)\s*[:\-]?\s*" + _NUMBER,
        re.IGNORECASE), "Basophils_Percent"),

    # Absolute counts (cells/µL or ×10³/µL)
    (re.compile(
        r"Neutrophils?\s+(?:Absolute|Abs\.?)\s*[:\-]?\s*" + _NUMBER,
        re.IGNORECASE), "Neutrophils_Absolute"),
    (re.compile(
        r"Lymphocytes?\s+(?:Absolute|Abs\.?)\s*[:\-]?\s*" + _NUMBER,
        re.IGNORECASE), "Lymphocytes_Absolute"),
    (re.compile(
        r"Eosinophils?\s+(?:Absolute|Abs\.?)\s*[:\-]?\s*" + _NUMBER,
        re.IGNORECASE), "Eosinophils_Absolute"),
    (re.compile(
        r"Monocytes?\s+(?:Absolute|Abs\.?)\s*[:\-]?\s*" + _NUMBER,
        re.IGNORECASE), "Monocytes_Absolute"),
    (re.compile(
        r"Basophils?\s+(?:Absolute|Abs\.?)\s*[:\-]?\s*" + _NUMBER,
        re.IGNORECASE), "Basophils_Absolute"),

    # Platelet Count
    (re.compile(
        r"(?:Platelet(?:\s+Count)?|PLT|Plt(?:\s+Count)?|PLATELET(?:\s+COUNT)?)\s*[:\-]?\s*" + _NUMBER,
        re.IGNORECASE), "Platelet_Count"),

    # MPV
    (re.compile(r"\bMPV\b\s*[:\-]?\s*" + _NUMBER, re.IGNORECASE), "MPV"),

    # ESR
    (re.compile(
        r"(?:E\.?S\.?R\.?(?:\*)?|Erythrocyte\s+Sedimentation\s+Rate(?:\s*\(ESR\))?)\s*[:\-]?\s*" + _NUMBER,
        re.IGNORECASE), "ESR"),

    # Blood Sugar Random
    (re.compile(
        r"(?:Blood\s+(?:Sugar|Glucose)\s+Random|Random\s+Blood\s+(?:Sugar|Glucose)|BSR|RBS|BLOOD\s+GLUCOSE\s+RANDOM)\s*[:\-]?\s*" + _NUMBER,
        re.IGNORECASE), "Blood_Sugar_Random"),

    # Serum Creatinine
    (re.compile(
        r"(?:S\.?\s*Creatinine|Serum\s+Creatinine|CREATININE)\s*[:\-]?\s*" + _NUMBER,
        re.IGNORECASE), "Serum_Creatinine"),

    # SGOT / AST
    (re.compile(
        r"(?:SGOT(?:/AST)?|S\.?G\.?O\.?T\.?(?:[,/]\s*AST)?|AST)\s*[:\-]?\s*" + _NUMBER,
        re.IGNORECASE), "SGOT_AST"),

    # SGPT / ALT
    (re.compile(
        r"(?:SGPT(?:/ALT)?|S\.?G\.?P\.?T\.?(?:[,/]\s*ALT)?|ALT)\s*[:\-]?\s*" + _NUMBER,
        re.IGNORECASE), "SGPT_ALT"),
]

# Patient identity patterns (value captured in group 1)
_PATIENT_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"(?:Age)\s*[:\-]?\s*(\d+)\s*(?:Y(?:rs?|ears?)?)?", re.IGNORECASE), "Age"),
    (re.compile(r"(?:Height|Ht\.?)\s*[:\-]?\s*" + _NUMBER, re.IGNORECASE), "Height"),
    (re.compile(r"(?:Weight|Wt\.?)\s*[:\-]?\s*" + _NUMBER, re.IGNORECASE), "Weight"),
    (re.compile(r"BMI\s*[:\-]?\s*" + _NUMBER, re.IGNORECASE), "BMI"),
    (re.compile(r"(?:Pulse(?:\s+Rate)?|PR)\s*[:\-]?\s*(\d+)", re.IGNORECASE), "Pulse"),
]

# Blood pressure — special format: "120/80"
_BP_PATTERN = re.compile(
    r"(?:BP|Blood\s+Pressure)\s*[:\-]?\s*(\d{2,3}\s*/\s*\d{2,3})",
    re.IGNORECASE,
)

# Patient name — common Indian lab report formats
_NAME_PATTERN = re.compile(
    r"(?:Patient\s+Name|Name\s+of\s+Patient|Patient|Name)\s*[:\-]\s*([A-Z][A-Za-z\s\.]{2,50}?)(?=\n|\r|$|\s{2,}|Age|DOB|Sex|Gender|Mr\.|Mrs\.)",
    re.MULTILINE,
)

# Gender
_GENDER_PATTERN = re.compile(
    r"(?:Gender|Sex)\s*[:\-]?\s*(Male|Female|M|F|MALE|FEMALE)",
    re.IGNORECASE,
)

# Blood Group: A / B / AB / O (with optional Rh)
_BLOOD_GROUP_PATTERN = re.compile(
    r"(?:Blood\s+Group(?:\s*\(ABO\))?|ABO\s+Group|BLOOD\s+GROUP)\s*[:\-]?\s*(AB|A|B|O)(?:\s*(Positive|\+|Negative|-))?",
    re.IGNORECASE,
)

# Rh factor standalone
_RH_PATTERN = re.compile(
    r"(?:Rh(?:\s+(?:Factor|Type))?|RHESUS)\s*[:\-]?\s*(Positive|\+|Negative|-)",
    re.IGNORECASE,
)

# Employee / UHID identity codes
_EMPCODE_PATTERN = re.compile(
    r"(?:Emp(?:loyee)?(?:\s+(?:Code|ID|No\.?))?|EMP(?:CODE|ID|NO)?)\s*[:\-]?\s*([A-Za-z0-9\-_/]{2,20})",
    re.IGNORECASE,
)
_UHID_PATTERN = re.compile(
    r"(?:UHID(?:\s*(?:No\.?|Number))?|U\.?H\.?I\.?D\.?)\s*[:\-]?\s*([A-Za-z0-9\-_/]{2,20})",
    re.IGNORECASE,
)


def _normalize_rh(raw: str) -> Optional[str]:
    if not raw:
        return None
    u = raw.strip().upper()
    if u in ("+", "POSITIVE", "POS"):
        return "Positive"
    if u in ("-", "NEGATIVE", "NEG"):
        return "Negative"
    return None


def _normalize_blood_group(raw: str) -> Optional[str]:
    if not raw:
        return None
    u = raw.strip().upper()
    if u in ("A", "B", "AB", "O"):
        return u
    return None


# ── Main extraction function ───────────────────────────────────────────────────

def extract_with_regex(page_text: str) -> dict:
    """
    Run deterministic regex extraction on a single PDF page's text.

    Called once per page. Returns a dict with the same field names as
    the Claude JSON schema. All unmatched fields are explicitly None.
    Flag fields are "HIGH", "LOW", or None — never embedded strings.
    Never returns a value like "12.6 L" — value and flag always separated.

    Use count_regex_fields(result) to decide routing:
      >= 3 non-null fields → REGEX_HANDLED (skip Claude for this page)
      <  3 non-null fields → CLAUDE_HANDLED (send page to Claude vision)
    """
    result: dict = {}

    # ── Numeric lab values ─────────────────────────────────────────────────────
    for pattern, field in _NUMERIC_PATTERNS:
        value, flag = _extract_value_and_flag(page_text, pattern, field)
        result[field] = value
        result[f"{field}_Flag"] = flag

    # ── Patient identity — numeric ─────────────────────────────────────────────
    for pattern, field in _PATIENT_PATTERNS:
        value, _ = _extract_value_and_flag(page_text, pattern, field)
        result[field] = value

    # ── Blood Pressure ─────────────────────────────────────────────────────────
    bp_match = _BP_PATTERN.search(page_text)
    result["BP"] = bp_match.group(1).replace(" ", "") if bp_match else None

    # ── Patient Name ───────────────────────────────────────────────────────────
    name_match = _NAME_PATTERN.search(page_text)
    if name_match:
        name = name_match.group(1).strip()
        if len(name) >= 3 and not re.match(r"^(Report|Lab|Date|Test)$", name, re.IGNORECASE):
            result["PatientName"] = name
        else:
            result["PatientName"] = None
    else:
        result["PatientName"] = None

    # ── Gender ─────────────────────────────────────────────────────────────────
    gender_match = _GENDER_PATTERN.search(page_text)
    if gender_match:
        g = gender_match.group(1).upper()
        result["Gender"] = "Male" if g in ("M", "MALE") else "Female"
    else:
        result["Gender"] = None

    # ── Blood Group ────────────────────────────────────────────────────────────
    bg_match = _BLOOD_GROUP_PATTERN.search(page_text)
    if bg_match:
        result["Blood_Group"] = _normalize_blood_group(bg_match.group(1))
        rh_raw = bg_match.group(2) if bg_match.lastindex and bg_match.lastindex >= 2 else None
        result["Rh_Type"] = _normalize_rh(rh_raw) if rh_raw else None
    else:
        result["Blood_Group"] = None
        result["Rh_Type"] = None

    # ── Rh Type (standalone, if not already captured) ─────────────────────────
    if not result.get("Rh_Type"):
        rh_match = _RH_PATTERN.search(page_text)
        result["Rh_Type"] = _normalize_rh(rh_match.group(1)) if rh_match else None

    # ── EmpCode ────────────────────────────────────────────────────────────────
    emp_match = _EMPCODE_PATTERN.search(page_text)
    result["EmpCode"] = emp_match.group(1).strip() if emp_match else None

    # ── UHIDNo ─────────────────────────────────────────────────────────────────
    uhid_match = _UHID_PATTERN.search(page_text)
    result["UHIDNo"] = uhid_match.group(1).strip() if uhid_match else None

    # ── Fields not handled by regex — AI's domain ─────────────────────────────
    for field in (
        "Mobile", "Remarks",
        "Urine_Colour", "Urine_Transparency", "Urine_PH",
        "Urine_Protein_Albumin", "Urine_Glucose", "Urine_Bilirubin",
        "Urine_Blood", "Urine_RBC",
        "Urine_Casts", "Urine_Crystals",
        "Urine_Specific_Gravity",
        "AUDIOMETRY", "PFT", "XRAY",
        "Suggestion",
    ):
        result.setdefault(field, None)

    return result


def count_regex_fields(regex_result: dict) -> int:
    """
    Count non-null, non-flag fields in a regex result dict.
    Used by batch_processor to decide REGEX_HANDLED vs CLAUDE_HANDLED.
    Flag fields (_Flag suffix) are excluded from the count.
    """
    return sum(
        1 for k, v in regex_result.items()
        if not k.endswith("_Flag") and v is not None
    )
