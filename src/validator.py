"""
validator.py

Post-extraction validation and cleaning layer.

Responsibilities:
  - Enforce MASTER_COLUMNS as the single source of truth for field names and order.
  - Coerce numeric fields to float; reject garbage strings.
  - Normalize flag fields to HIGH / LOW / None.
  - Split embedded flags (e.g. "12.6 L") into value + flag.
  - Validate Blood_Group and Rh_Type.
  - Fallback PatientName to filename stem if missing.
  - Run clinical plausibility checks and populate Data_Quality column.

No confidence scoring — regex and AI never overlap on the same page,
so there is nothing to compare. The strict handoff in batch_processor
(regex >= 3 fields → page done, AI never called) makes conflicts
structurally impossible.
"""

import logging
import re
from typing import Any, Optional

logger = logging.getLogger(__name__)

# ── Master column list ─────────────────────────────────────────────────────────
# Single source of truth for field names, order, and Excel columns.
# No Status column. No _Confidence columns. Remarks (not Remark) is one field.

MASTER_COLUMNS = [
    # Identity
    "EmpCode", "UHIDNo", "PatientName",
    # Demographics
    "Age", "Gender", "Height", "Weight", "BMI", "BP", "Pulse", "Mobile",
    # Biochemistry
    "Blood_Sugar_Random", "Blood_Sugar_Random_Flag",
    # Blood group
    "Blood_Group", "Rh_Type",
    # CBC — red cell
    "Haemoglobin", "Haemoglobin_Flag",
    "Red_Blood_Cell_Count", "Red_Blood_Cell_Count_Flag",
    "Hct", "Hct_Flag",
    "MCV", "MCV_Flag",
    "MCH", "MCH_Flag",
    "MCHC", "MCHC_Flag",
    "RDW_CV", "RDW_CV_Flag",
    "RDW_SD", "RDW_SD_Flag",
    # CBC — white cell
    "TLC", "TLC_Flag",
    "Neutrophil_Percent", "Neutrophil_Percent_Flag",
    "Lymphocyte_Percent", "Lymphocyte_Percent_Flag",
    "Eosinophils_Percent", "Eosinophils_Percent_Flag",
    "Monocytes_Percent", "Monocytes_Percent_Flag",
    "Basophils_Percent", "Basophils_Percent_Flag",
    # CBC — absolute counts
    "Neutrophils_Absolute",
    "Lymphocytes_Absolute",
    "Eosinophils_Absolute",
    "Monocytes_Absolute",
    "Basophils_Absolute",
    # CBC — platelets / other
    "Platelet_Count", "Platelet_Count_Flag",
    "MPV", "MPV_Flag",
    "ESR", "ESR_Flag",
    # Biochemistry continued
    "Serum_Creatinine", "Serum_Creatinine_Flag",
    "SGOT_AST", "SGOT_AST_Flag",
    "SGPT_ALT", "SGPT_ALT_Flag",
    # Urine
    "Urine_Colour", "Urine_Transparency",
    "Urine_Protein_Albumin", "Urine_Glucose", "Urine_Bilirubin",
    "Urine_Blood", "Urine_Casts", "Urine_Crystals", "Urine_RBC",
    "Urine_PH", "Urine_Specific_Gravity",
    # Speciality tests
    "AUDIOMETRY", "PFT", "XRAY",
    # Free text
    "Remarks", "Suggestion",
    # Meta
    "Extraction_Note",
    "Data_Quality",
]

# ── Excel display names ────────────────────────────────────────────────────────
# Flags are NOT separate columns in Excel — they are embedded in the value cell
# as "12.6 (L)" or "12.6 (H)" by the Excel writer.
# _Flag columns are internal only and excluded from Excel output.

COLUMN_DISPLAY_NAMES = {
    "EmpCode":                  "EmpCode",
    "UHIDNo":                   "UHIDNo.",
    "PatientName":              "PatientName",
    "Age":                      "Age",
    "Gender":                   "Gender",
    "Height":                   "Height",
    "Weight":                   "Weight",
    "BMI":                      "BMI",
    "BP":                       "BP",
    "Pulse":                    "Pulse",
    "Mobile":                   "Mobile",
    "Blood_Sugar_Random":       "Blood Sugar Random",
    "Blood_Group":              "Blood Group",
    "Rh_Type":                  "Rh Type",
    "Haemoglobin":              "Haemoglobin",
    "Red_Blood_Cell_Count":     "Red Blood Cell Count",
    "Hct":                      "Hct",
    "MCV":                      "MCV",
    "MCH":                      "MCH",
    "MCHC":                     "MCHC",
    "RDW_CV":                   "RDW - CV",
    "RDW_SD":                   "RDW - SD",
    "TLC":                      "TLC",
    "Neutrophil_Percent":       "Neutrophil %",
    "Lymphocyte_Percent":       "Lymphocyte %",
    "Eosinophils_Percent":      "Eosinophils %",
    "Monocytes_Percent":        "Monocytes %",
    "Basophils_Percent":        "Basophils %",
    "Neutrophils_Absolute":     "Neutrophils (Abs)",
    "Lymphocytes_Absolute":     "Lymphocytes (Abs)",
    "Eosinophils_Absolute":     "Eosinophils (Abs)",
    "Monocytes_Absolute":       "Monocytes (Abs)",
    "Basophils_Absolute":       "Basophils (Abs)",
    "Platelet_Count":           "Platelet Count",
    "MPV":                      "MPV",
    "ESR":                      "ESR",
    "Serum_Creatinine":         "Serum Creatinine",
    "SGOT_AST":                 "SGOT / AST",
    "SGPT_ALT":                 "SGPT / ALT",
    "Urine_Colour":             "Colour",
    "Urine_Transparency":       "Transparency",
    "Urine_Protein_Albumin":    "Protein (Albumin)",
    "Urine_Glucose":            "Glucose",
    "Urine_Bilirubin":          "Bilirubin",
    "Urine_Blood":              "Blood",
    "Urine_Casts":              "Casts",
    "Urine_Crystals":           "Crystals",
    "Urine_RBC":                "RBC",
    "Urine_PH":                 "PH",
    "Urine_Specific_Gravity":   "Specific Gravity",
    "AUDIOMETRY":               "AUDIOMETRY",
    "PFT":                      "PFT",
    "XRAY":                     "X-RAY",
    "Remarks":                  "Remarks",
    "Suggestion":               "Suggestion",
    "Extraction_Note":          "Extraction Note",
    "Data_Quality":             "Data Quality",
}

# ── Field category sets ────────────────────────────────────────────────────────

NUMERIC_FIELDS = {
    "Age", "Height", "Weight", "BMI", "Pulse",
    "Haemoglobin", "Red_Blood_Cell_Count", "Hct",
    "MCV", "MCH", "MCHC", "RDW_CV", "RDW_SD",
    "TLC",
    "Neutrophil_Percent", "Lymphocyte_Percent",
    "Eosinophils_Percent", "Monocytes_Percent", "Basophils_Percent",
    "Neutrophils_Absolute", "Lymphocytes_Absolute",
    "Eosinophils_Absolute", "Monocytes_Absolute", "Basophils_Absolute",
    "Platelet_Count", "MPV", "ESR",
    "Blood_Sugar_Random",
    "Serum_Creatinine", "SGOT_AST", "SGPT_ALT",
    "Urine_PH", "Urine_Specific_Gravity",
}

FLAG_FIELDS = {col for col in MASTER_COLUMNS if col.endswith("_Flag")}

VALID_BLOOD_GROUPS = {"A", "B", "AB", "O"}

# ── Clinical plausibility ranges ───────────────────────────────────────────────
# (min_plausible, max_plausible) — outside these = likely extraction error

_PLAUSIBILITY_RANGES: dict[str, tuple[float, float]] = {
    "Haemoglobin":          (3.0,    25.0),
    "Red_Blood_Cell_Count": (1.0,    10.0),
    "Hct":                  (5.0,    65.0),
    "MCV":                  (50.0,   130.0),
    "MCH":                  (10.0,   50.0),
    "MCHC":                 (20.0,   40.0),
    "TLC":                  (0.5,    100.0),
    "Platelet_Count":       (10.0,   1500.0),
    "Neutrophil_Percent":   (0.0,    100.0),
    "Lymphocyte_Percent":   (0.0,    100.0),
    "Eosinophils_Percent":  (0.0,    60.0),
    "Monocytes_Percent":    (0.0,    30.0),
    "Basophils_Percent":    (0.0,    10.0),
    "ESR":                  (0.0,    150.0),
    "Blood_Sugar_Random":   (20.0,   700.0),
    "Serum_Creatinine":     (0.1,    20.0),
    "SGOT_AST":             (5.0,    2000.0),
    "SGPT_ALT":             (5.0,    2000.0),
    "Age":                  (1.0,    120.0),
    "BMI":                  (10.0,   70.0),
    "Pulse":                (30.0,   220.0),
}

# Differential fields — should sum to ~100%
_DIFFERENTIAL_FIELDS = [
    "Neutrophil_Percent",
    "Lymphocyte_Percent",
    "Eosinophils_Percent",
    "Monocytes_Percent",
    "Basophils_Percent",
]
_DIFFERENTIAL_SUM_TOLERANCE = 10.0   # acceptable deviation from 100%


# ── Internal helpers ───────────────────────────────────────────────────────────

_EMBEDDED_FLAG_RE = re.compile(
    r"^([\d.]+)\s*(L|H|Low|High|low|high)$", re.IGNORECASE
)


def _strip_str(value: Any) -> Any:
    if isinstance(value, str):
        return value.strip()
    return value


def _parse_embedded_flag(raw: str) -> tuple[Optional[float], Optional[str]]:
    match = _EMBEDDED_FLAG_RE.match(raw.strip())
    if match:
        num_str, flag_char = match.group(1), match.group(2).upper()
        try:
            num_val = float(num_str)
            flag = "LOW" if flag_char in ("L", "LOW") else "HIGH"
            return num_val, flag
        except ValueError:
            return None, None
    return None, None


def _coerce_numeric(value: Any, field_name: str) -> tuple[Any, Optional[str]]:
    """
    Coerce value to float for numeric fields.
    Returns (coerced_value, flag_hint_or_None).
    If value contains an embedded flag ("12.6 L"), splits it out.
    """
    if value is None:
        return None, None
    if isinstance(value, (int, float)):
        return value, None
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return None, None
        num_val, flag_hint = _parse_embedded_flag(stripped)
        if num_val is not None:
            logger.warning(
                f"Field '{field_name}' had embedded flag '{stripped}'. "
                f"Extracted numeric={num_val}, flag={flag_hint}"
            )
            return num_val, flag_hint
        try:
            return float(stripped), None
        except ValueError:
            logger.warning(
                f"Field '{field_name}' expected numeric but got '{stripped}'. Setting null."
            )
            return None, None
    logger.warning(
        f"Field '{field_name}' unexpected type {type(value).__name__}. Setting null."
    )
    return None, None


def _normalize_flag(value: Any, field_name: str) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, str):
        upper = value.strip().upper()
        if upper in ("HIGH", "H"):
            return "HIGH"
        if upper in ("LOW", "L"):
            return "LOW"
        if upper == "":
            return None
        logger.warning(f"Flag '{field_name}' unexpected value '{value}'. Setting null.")
        return None
    logger.warning(
        f"Flag '{field_name}' expected string, got {type(value).__name__}. Setting null."
    )
    return None


# ── Clinical plausibility checks ──────────────────────────────────────────────

def run_data_quality_checks(data: dict) -> str:
    """
    Run clinical plausibility checks on an extracted patient record.
    Returns a pipe-separated string of issues, or empty string if all clear.

    Checks:
      1. Numeric fields outside plausibility ranges.
      2. Differential WBC percentages sum (should be ~100%).
      3. Blood_Group present but Rh_Type null.
      4. BMI cross-check against Height and Weight if all three present.
    """
    issues: list[str] = []

    # Check 1: Plausibility ranges
    for field, (lo, hi) in _PLAUSIBILITY_RANGES.items():
        val = data.get(field)
        if val is None:
            continue
        try:
            fval = float(val)
            if not (lo <= fval <= hi):
                issues.append(f"{field}={fval} outside expected {lo}-{hi}")
        except (TypeError, ValueError):
            pass

    # Check 2: Differential sum
    diff_vals = []
    for f in _DIFFERENTIAL_FIELDS:
        v = data.get(f)
        if v is not None:
            try:
                diff_vals.append(float(v))
            except (TypeError, ValueError):
                pass

    if len(diff_vals) >= 3:
        diff_sum = sum(diff_vals)
        if abs(diff_sum - 100.0) > _DIFFERENTIAL_SUM_TOLERANCE:
            issues.append(f"Differential sum={diff_sum:.1f}% (expected ~100%)")

    # Check 3: Blood Group without Rh Type
    if data.get("Blood_Group") and not data.get("Rh_Type"):
        issues.append("Blood_Group present but Rh_Type missing")

    # Check 4: BMI cross-check
    height_cm = data.get("Height")
    weight_kg = data.get("Weight")
    bmi       = data.get("BMI")
    if height_cm and weight_kg and bmi:
        try:
            h_m = float(height_cm) / 100.0
            calc_bmi = float(weight_kg) / (h_m ** 2)
            if abs(calc_bmi - float(bmi)) > 3.0:
                issues.append(
                    f"BMI={bmi} vs calculated={calc_bmi:.1f} from H={height_cm}/W={weight_kg}"
                )
        except (TypeError, ValueError, ZeroDivisionError):
            pass

    return " | ".join(issues) if issues else ""


# ── Main public API ────────────────────────────────────────────────────────────

def validate_and_clean(
    raw_data: dict,
    filename: str,
    existing_note: str = "",
) -> dict:
    """
    Validate and clean an extracted patient record.

    Steps:
      1. Ensure all MASTER_COLUMNS keys present (fill missing with None).
      2. Flatten any nested dicts the AI may have returned.
      3. Strip whitespace from all string fields.
      4. Coerce numeric fields to float; split embedded flags.
      5. Normalize flag fields to HIGH / LOW / None.
      6. PatientName fallback to filename stem.
      7. Blood_Group and Rh_Type normalization.
      8. Clinical plausibility checks -> Data_Quality column.
      9. Finalize Extraction_Note.

    Returns a clean dict with all MASTER_COLUMNS keys present.
    """
    notes: list[str] = []
    if existing_note:
        notes.append(existing_note)

    # ── Step 1 & 2: Ensure all keys; flatten nested AI dicts ──────────────────
    cleaned: dict = {}
    for col in MASTER_COLUMNS:
        val = raw_data.get(col, None)
        if isinstance(val, dict) and "value" in val:
            cleaned[col] = val.get("value")
            flag_col = f"{col}_Flag"
            if flag_col in FLAG_FIELDS and val.get("flag"):
                cleaned[flag_col] = val["flag"]
        else:
            cleaned[col] = val

    # ── Step 3: Strip whitespace ───────────────────────────────────────────────
    for col in MASTER_COLUMNS:
        cleaned[col] = _strip_str(cleaned[col])

    # ── Step 4: Numeric coercion ───────────────────────────────────────────────
    for f in NUMERIC_FIELDS:
        if f not in cleaned:
            continue
        coerced, flag_hint = _coerce_numeric(cleaned[f], f)
        cleaned[f] = coerced
        if flag_hint:
            flag_field = f"{f}_Flag"
            if flag_field in FLAG_FIELDS and cleaned.get(flag_field) is None:
                cleaned[flag_field] = flag_hint
                logger.info(f"Auto-populated {flag_field}='{flag_hint}' from embedded value.")

    # ── Step 5: Flag normalization ─────────────────────────────────────────────
    for f in FLAG_FIELDS:
        if f in cleaned:
            cleaned[f] = _normalize_flag(cleaned[f], f)

    # ── Step 6: PatientName fallback ───────────────────────────────────────────
    if not cleaned.get("PatientName"):
        stem = filename[:-4] if filename.lower().endswith(".pdf") else filename
        cleaned["PatientName"] = stem
        notes.append("NAME_NOT_FOUND")
        logger.info(f"PatientName not found; using filename: {stem}")

    # ── Step 7: Blood_Group validation ────────────────────────────────────────
    raw_bg = cleaned.get("Blood_Group")
    if raw_bg is not None:
        bg_upper = str(raw_bg).strip().upper()
        rh_detected = None

        if bg_upper.endswith("+"):
            rh_detected = "Positive"
            bg_upper = bg_upper[:-1].strip()
        elif bg_upper.endswith("-"):
            rh_detected = "Negative"
            bg_upper = bg_upper[:-1].strip()
        elif bg_upper.endswith("POSITIVE"):
            rh_detected = "Positive"
            bg_upper = bg_upper[:-8].strip()
        elif bg_upper.endswith("NEGATIVE"):
            rh_detected = "Negative"
            bg_upper = bg_upper[:-8].strip()

        if bg_upper in VALID_BLOOD_GROUPS:
            cleaned["Blood_Group"] = bg_upper
            if rh_detected and not cleaned.get("Rh_Type"):
                cleaned["Rh_Type"] = rh_detected
        else:
            logger.warning(f"Invalid Blood_Group '{raw_bg}' for {filename}. Setting null.")
            cleaned["Blood_Group"] = None

    # ── Step 8: Rh_Type normalization ──────────────────────────────────────────
    raw_rh = cleaned.get("Rh_Type")
    if raw_rh is not None:
        rh_str = str(raw_rh).strip().upper()
        if rh_str in ("+", "POSITIVE", "POS", "RH+", "RH POSITIVE"):
            cleaned["Rh_Type"] = "Positive"
        elif rh_str in ("-", "NEGATIVE", "NEG", "RH-", "RH NEGATIVE"):
            cleaned["Rh_Type"] = "Negative"
        else:
            logger.warning(f"Unrecognized Rh_Type '{raw_rh}' for {filename}. Keeping as-is.")

    # ── Step 9: Clinical plausibility -> Data_Quality ──────────────────────────
    quality_note = run_data_quality_checks(cleaned)
    cleaned["Data_Quality"] = quality_note if quality_note else None

    # ── Finalize Extraction_Note ───────────────────────────────────────────────
    cleaned["Extraction_Note"] = " | ".join(notes) if notes else None

    return cleaned


def count_fields(data: dict) -> tuple[int, int]:
    """
    Returns (fields_extracted, fields_null).
    Excludes Extraction_Note, Data_Quality, and _Flag columns from the count.
    """
    countable = [
        col for col in MASTER_COLUMNS
        if col not in ("Extraction_Note", "Data_Quality")
        and not col.endswith("_Flag")
    ]
    extracted = sum(1 for col in countable if data.get(col) is not None)
    return extracted, len(countable) - extracted
