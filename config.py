import os
from dotenv import load_dotenv

load_dotenv()

ENV = os.getenv("ENV", "dev")
if ENV not in ("dev", "production"):
    raise ValueError(f"Invalid ENV value: '{ENV}'. Must be 'dev' or 'production'.")

# ── OpenAI ─────────────────────────────────────────────────────────────────────
MODEL          = "gpt-4o"
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
if not OPENAI_API_KEY and os.getenv("MOCK_AI", "false").lower() != "true":
    raise ValueError(
        "OPENAI_API_KEY is not set. Add OPENAI_API_KEY=sk-... to your .env file. "
        "Set MOCK_AI=true to run without an API key."
    )

# ── API authentication ─────────────────────────────────────────────────────────
# Set API_KEY in .env. All non-static endpoints require:
#   Header: X-API-Key: <value>
# If not set the app will refuse to start.
API_KEY: str = os.getenv("API_KEY", "")
if not API_KEY:
    raise ValueError(
        "API_KEY is not set. Add API_KEY=<secret> to your .env file."
    )

# ── Persistent job store ───────────────────────────────────────────────────────
# PostgreSQL connection string. Set in .env:
#   DATABASE_URL=postgresql://user:password@host:5432/medextract
# Works with Railway, Render, Supabase, AWS RDS — any Postgres provider.
DATABASE_URL: str = os.getenv("DATABASE_URL", "")
if not DATABASE_URL:
    raise ValueError(
        "DATABASE_URL is not set. Add DATABASE_URL=postgresql://... to your .env file."
    )

# ── Concurrency ────────────────────────────────────────────────────────────────
# Number of PDFs processed simultaneously (asyncio semaphore in run_batch).
# For 300 docs/day: 10-12 concurrent PDFs recommended.
MAX_CONCURRENT_EXTRACTIONS: int = int(os.getenv("MAX_WORKERS", "10"))

# Max concurrent OpenAI API calls in flight at any one time.
# With chunked AI calls (batch_processor Phase 2), each call covers 3-4 pages,
# so there are far fewer calls in flight than before. Raising to 10 allows all
# MAX_CONCURRENT_EXTRACTIONS files to run their chunks truly in parallel while
# staying well within Tier 1 limits (~150 sustained RPM vs the 500 RPM cap).
# Raise to 30-40 on Tier 2 (5000 RPM).
AI_CONCURRENCY: int = int(os.getenv("AI_CONCURRENCY", "10"))

# ── Directories ────────────────────────────────────────────────────────────────
OUTPUT_DIR = os.path.join(os.path.dirname(__file__), "output")
os.makedirs(OUTPUT_DIR, exist_ok=True)

UPLOAD_DIR = os.path.join(os.path.dirname(__file__), "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)

# ── PDF processing thresholds ──────────────────────────────────────────────────
TEXT_MODE_MIN_CHARS  = 100   # pages with fewer chars than this → OCR mode
GRAPH_PAGE_MAX_CHARS = 200   # graph pages expected to have very little text

# Graph-related keywords that trigger GRAPH_PAGE detection
GRAPH_KEYWORDS = [
    "ECG", "EKG", "electrocardiogram",
    "audiogram", "audiometry graph",
    "spirometry curve", "flow volume",
    "TMT", "treadmill", "waveform",
]

# ── OCR ────────────────────────────────────────────────────────────────────────
# PDF-to-image render resolution for scanned pages
OCR_DPI = 200

# Minimum mean engine confidence to trust OCR output and run regex on it.
# Below this threshold the page goes straight to AI (image mode).
# Range 0.0–1.0. Higher threshold = more pages sent to OpenAI for accuracy.
OCR_CONFIDENCE_THRESHOLD: float = float(os.getenv("OCR_CONFIDENCE_THRESHOLD", "0.7"))

# OCR engine — Tesseract only for lighter, faster processing.
# OpenAI handles low-confidence cases automatically.
OCR_ENGINE_PRIMARY: str = os.getenv("OCR_ENGINE_PRIMARY", "tesseract")

# Fallback engine (same as primary since PaddleOCR removed).
OCR_ENGINE_FALLBACK: str = os.getenv("OCR_ENGINE_FALLBACK", "tesseract")

# ── AI ─────────────────────────────────────────────────────────────────────────
# Retry attempts on JSON parse failure or network error (0 = no retry).
AI_MAX_RETRIES: int = 1

# ── Mock mode ──────────────────────────────────────────────────────────────────
# Set MOCK_AI=true in .env to skip all OpenAI API calls.
# Pipeline runs fully (PDF, OCR, regex, DB writes, Excel) but AI calls
# return empty dict with note "LLM_MOCK". Fields AI would fill stay null.
# Use for local testing and CI — no tokens consumed, no API key needed.
MOCK_AI: bool = os.getenv("MOCK_AI", "false").lower() == "true"
