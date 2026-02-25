# MedExtract — Medical Report Intelligence Platform

A production-ready pipeline for extracting structured data from Indian lab report PDFs using OpenAI. Each PDF page is processed independently using a three-layer routing strategy: deterministic regex handles structured pages for free, local OCR (Tesseract) handles scanned pages without AI where confidence permits, and OpenAI vision is invoked only when local layers cannot decode a page. Processes multiple PDFs in parallel and exports a clean, single-sheet Excel workbook. Job state persists in PostgreSQL — survives server restarts and supports multiple workers.

**🔥 OPTIMIZED: Tesseract-only OCR** — Lighter & faster build. PaddleOCR removed. OpenAI handles low-confidence cases.
**🔐 NEW: Secure Authentication** — Login required with username/password. API key no longer exposed to frontend.

---

## 🔐 Authentication

**Default Credentials:**
```
Username: admin
Password: Admin@321
```

**How to Login:**
1. Start the application: `docker-compose up -d`
2. Open browser: `http://localhost:8000`
3. Enter credentials on login screen
4. Session lasts **8 hours**
5. Logout button in top-right corner

**Security Features:**
- ✅ API key protected server-side (removed from frontend)
- ✅ Session-based authentication with token expiration
- ✅ All API endpoints require valid session token
- ✅ Automatic logout on session expiry
- ✅ Backward compatible: accepts API key OR session token

📖 **Full documentation**: See [AUTHENTICATION_AND_SCALING.md](AUTHENTICATION_AND_SCALING.md)

---

## Project Structure

```
medical_extractor/
├── main.py                  # FastAPI app + endpoints
├── config.py                # ENV-driven config (AI, DB, OCR thresholds)
├── requirements.txt
├── .env                     # Local secrets (never commit)
├── static/
│   └── index.html           # Drag-and-drop frontend SPA
├── src/
│   ├── pdf_processor.py     # PDF → per-page text / OCR / graph detection
│   ├── regex_extractor.py   # Deterministic layer 1 extraction (free, no AI)
│   ├── ocr_extractor.py     # Layer 2: Tesseract OCR with confidence gating
│   ├── ai_extractor.py      # OpenAI API abstraction — focused per-page calls
│   ├── batch_processor.py   # Async job orchestration + page-level routing
│   ├── validator.py         # Field validation, confidence scoring, cleaning
│   ├── excel_writer.py      # 4-sheet Excel generation
│   └── _db.py               # PostgreSQL persistence layer (job state + Excel bytes)
└── output/                  # Generated Excel files (auto-created, legacy)
```

---

## Quick Start

### 1. Install system dependencies

`pdf2image` requires Poppler and the OCR fallback requires Tesseract:

```bash
# macOS
brew install poppler tesseract

# Ubuntu / Debian
sudo apt-get install poppler-utils tesseract-ocr
```

### 2. Install Python dependencies

```bash
cd medical_extractor
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

**Note:** PaddleOCR has been removed for a lighter, faster build. Tesseract handles all OCR needs, with OpenAI as the AI layer for low-confidence pages.

### 3. Provision PostgreSQL

Any Postgres provider works — Railway, Render, Supabase, AWS RDS all provide a `DATABASE_URL` on their free tier. For local development:

```bash
# Docker (quickest)
docker run -d --name medextract-db \
  -e POSTGRES_DB=medextract \
  -e POSTGRES_USER=med \
  -e POSTGRES_PASSWORD=secret \
  -p 5432:5432 postgres:16
```

### 4. Configure environment

Edit `.env`:

```
ENV=dev
OPENAI_API_KEY=sk-...
API_KEY=change-me-to-a-strong-random-secret
DATABASE_URL=postgresql://med:secret@localhost:5432/medextract

# Optional OCR tuning (defaults shown)
OCR_CONFIDENCE_THRESHOLD=0.8    # 0.0–1.0. Higher = more AI calls for accuracy
OCR_ENGINE_PRIMARY=tesseract    # tesseract only (PaddleOCR removed)
OCR_ENGINE_FALLBACK=tesseract   # same as primary
```

`API_KEY` and `DATABASE_URL` are both required — the server refuses to start without either. Schema is created automatically on first start.

### 5. Run the server

```bash
python main.py
```

Or with uvicorn directly:

```bash
uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```

Open `http://localhost:8000` in your browser.

---

## Switching to Production

The system now uses OpenAI by default for both development and production. Simply set your `OPENAI_API_KEY` in the `.env` file:

```
ENV=production
OPENAI_API_KEY=sk-...
```

The app uses the `gpt-4o` model for both environments.

---

## How It Works

### Per-PDF pipeline

```
PDF bytes
  └─► pdf_processor.py
        ├─ Graph keyword + text < 200 chars  →  GRAPH_PAGE   (no PNG rendered)
        ├─ Text length > 100 chars           →  Text page    (PNG refs stored, NOT rendered yet)
        └─ Text length ≤ 100 chars           →  OCR page     (PNG rendered + local OCR run immediately)
              │
              ▼ (per page, in order)
        batch_processor.py — page router
              │
              ├─ GRAPH_PAGE:
              │    Mark field PRESENT. No AI call. No PNG. Free.
              │
              ├─ REGEX_HANDLED (text page, regex finds >= 3 fields):
              │    regex_extractor.py runs deterministic patterns.
              │    Merge into patient record. No AI call. No PNG rendered. Free.
              │
              ├─ OCR_HANDLED (scanned page, local OCR confidence >= threshold, regex >= 3 fields):
              │    ocr_extractor.py ran Tesseract during pdf_processor.
              │    regex_extractor.py runs on OCR text. Merge into patient record.
              │    No AI call. Free.
              │
              └─ AI_HANDLED (text page regex < 3 fields, OCR confidence < threshold,
                                  or OCR regex < 3 fields):
                   Partial regex/OCR values merged first (if any).
                   page.render_image() called NOW — PNG rendered on demand only for
                   pages that actually need AI.
                   Page PNG sent to AI with only the still-null fields.
                   ai_result merged into patient record.
              │
              ▼ (after all pages)
        Safety-net pass (ai_extractor.extract_safety_net)
              └─ One final text-only Claude call for fields still null after
                 the per-page loop. Only fires when the PDF has ≥ 100 chars of
                 real extractable text. Skipped entirely for fully-scanned PDFs.
              │
              ▼
        validator.calculate_confidence
              └─ Scores regex vs Claude agreement per field:
                 HIGH (both agree) / MEDIUM (one source) / LOW (conflict)
                 LOW on critical fields → flagged for Sheet 4 review
              │
              ▼
        validator.validate_and_clean
              └─ Type coercion, flag normalisation, PatientName fallback
              │
              ▼
        _db.py — persist file result to PostgreSQL
              │
              ▼
        excel_writer.build_excel
              └─ 4-sheet Excel workbook → stored in PostgreSQL as BYTEA
```

### Page routing decision

| Condition | Handler | Claude called? | Cost |
|-----------|---------|----------------|------|
| Graph keyword + text < 200 chars | GRAPH_PAGE | No | Free |
| Text page, regex finds ≥ 3 fields | REGEX_HANDLED | No | Free |
| Scanned page, OCR conf ≥ threshold, regex ≥ 3 fields | OCR_HANDLED | No | Free |
| Scanned page, OCR conf < threshold | CLAUDE_HANDLED | Yes — page PNG + null fields only | Minimal |
| Scanned page, OCR conf OK but regex < 3 fields | CLAUDE_HANDLED | Yes — page PNG + null fields only | Minimal |
| Text page, regex finds < 3 fields | CLAUDE_HANDLED | Yes — page PNG + null fields only | Minimal |

### Local OCR strategy

```
Scanned page → Tesseract OCR
                 → confidence ≥ 0.8  → regex
                                         → ≥ 3 fields: OCR_HANDLED (free)
                                         → < 3 fields: OpenAI
                 → confidence < 0.8  → OpenAI directly (no regex)
```

The 0.8 confidence threshold ensures accuracy. Low-confidence OCR text fed into regex produces silently wrong extractions — a worse outcome than an LLM miss. If Tesseract returns below-threshold confidence, the page goes straight to OpenAI with no regex attempt.

### Merge conflict rules

| Field type | Winner |
|------------|--------|
| Numeric lab values | Regex wins (deterministic, no hallucination) |
| Narrative text (XRAY, PFT, AUDIOMETRY, Suggestion, Remark) | Claude wins |
| Identity fields (Name, Age, Gender, dates) | Claude wins |
| Both sources found different values | LOW confidence → Sheet 4 review |

### Concurrency

Up to 5 PDFs processed concurrently (configurable via `MAX_CONCURRENT_EXTRACTIONS` in `config.py`). One PDF failing never stops others. Each PDF's per-page loop runs synchronously within its thread to preserve page order.

### Persistence

Job state is stored in PostgreSQL (`_db.py`). The connection pool (`ThreadedConnectionPool`) is safe for use inside `ThreadPoolExecutor` workers and from the async FastAPI event loop.

**Survives server restarts:** all job IDs, statuses, per-file stats, and generated Excel output. `GET /status/{id}` and `GET /download/{id}` work correctly after a restart.

**Multi-worker safe:** PostgreSQL handles concurrent writes correctly. SQLite was not used because it corrupts under concurrent writes from multiple uvicorn workers.

**Excel bytes** are stored as `BYTEA` in the `jobs` table — no separate file system required.

---

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/upload` | Upload one or more PDFs. Returns `{ "job_id": "..." }` |
| `GET`  | `/status/{job_id}` | Poll per-file progress and job state |
| `GET`  | `/download/{job_id}` | Download Excel when status is `complete` |
| `GET`  | `/` | Frontend SPA |

### Status response

```json
{
  "job_id": "uuid",
  "total": 10,
  "completed": 7,
  "failed": 1,
  "in_progress": 2,
  "status": "processing",
  "files": [
    { "filename": "patient1.pdf", "status": "done", "patient_name": "AARTI DEVI" }
  ]
}
```

---

## Excel Output

The downloaded file contains four sheets:

**Sheet 1 — Master Data**
One row per patient. 105 columns covering all extracted fields, flags, and confidence scores. Cell colours:
- 🟢 Green `#C6EFCE` — value present, within normal range, HIGH confidence
- 🟡 Yellow `#FFEB9C` — value present with HIGH or LOW flag, HIGH confidence
- 🟠 Orange `#F4B942` — value present, MEDIUM confidence (only one source found it)
- 🔴 Red `#FFC7CE` — LOW confidence (regex vs Claude conflict), or extraction error
- ⬜ Grey `#D3D3D3` — field not found in report (null)

**Sheet 2 — Abnormals Only**
Filtered view: only fields where a HIGH or LOW flag was detected.
Columns: `PatientName | Test | Value | Flag | Lab_Name | Report_Date`

**Sheet 3 — Processing Summary**
One row per PDF. Columns:
`Filename | PatientName | Status | Fields_Extracted | Fields_Null | Processing_Time_Seconds | Error_Notes | Pages_Regex_Handled | Pages_OCR_Handled | Pages_Claude_Handled | Pages_Graph_Detected | Targeted_Rescan_Used | Rescan_Pages_Sent | Conflicts_Found | Unrecovered_Fields`

- `Pages_Regex_Handled` — pages processed for free by regex on pdfplumber text
- `Pages_OCR_Handled` — scanned pages where local OCR + regex succeeded (no Claude)
- `Pages_Claude_Handled` — pages that required a Claude vision call
- `Pages_Graph_Detected` — graph pages skipped by both layers
- `Targeted_Rescan_Used` — whether targeted per-page rescan was triggered (orange if Yes)
- `Rescan_Pages_Sent` — number of pages re-sent during targeted rescan
- `Conflicts_Found` — number of critical fields with regex vs Claude disagreement
- `Unrecovered_Fields` — fields still null after all passes (red cell); operators should review the original PDF for these

**Sheet 4 — Review Required** *(only present when conflicts exist)*
One row per critical-field conflict. Columns:
`PatientName | Field | Regex_Value | Claude_Value | Conflict | Lab_Name | Report_Date`

Only written when at least one critical field has LOW confidence. Critical fields: `Haemoglobin`, `Blood_Group`, `SGOT_AST`, `SGPT_ALT`, `Serum_Creatinine`.

---

## Extracted Fields

The platform extracts and normalises across these categories (105 total output columns):

- **Patient info**: EmpCode, UHIDNo, PatientName, Age, Gender, Mobile, Height, Weight, BMI, BP, Pulse
- **CBC / Haematology**: Haemoglobin, RBC, Hct, MCV, MCH, MCHC, RDW-CV, RDW-SD, TLC, differential counts (absolute + percent for Neutrophil, Lymphocyte, Eosinophil, Monocyte, Basophil), Platelet, MPV, ESR
- **Biochemistry**: Blood Sugar Random, Serum Creatinine, SGOT/AST, SGPT/ALT
- **Urine**: Colour, Transparency, pH, Protein/Albumin, Glucose, Bilirubin, Blood, Pus Cells, RBC, Casts, Crystals, Epithelial Cells, Specific Gravity
- **Special investigations**: AUDIOMETRY, PFT (spirometry), XRAY (impression text)
- **Metadata**: Blood Group, Rh Type, Lab Name, Report Date

Every numeric field has a paired `_Flag` sibling (`HIGH` / `LOW` / null) and a `_Confidence` sibling (`HIGH` / `MEDIUM` / `LOW` / null).

---

## Error Handling

Every PDF produces a row in the Excel output — no silent failures.

| Condition | Extraction_Note |
|-----------|----------------|
| Password-protected PDF | `PDF_PASSWORD_PROTECTED` |
| Corrupted / unreadable PDF | `PDF_CORRUPTED` |
| Not a medical report | `NOT_MEDICAL_REPORT` |
| Partial OCR failure | `PARTIAL_OCR` |
| Patient name not found | `NAME_NOT_FOUND` |
| Claude API timeout / error | `API_ERROR` |
| Invalid JSON from Claude (after retry) | `API_ERROR` |

---

## Configuration Reference

All tuneable constants live in `config.py` and can be overridden via `.env`:

| Constant | Default | Description |
|----------|---------|-------------|
| `API_KEY` | *(required)* | Secret for `X-API-Key` header auth |
| `DATABASE_URL` | *(required)* | PostgreSQL connection string |
| `MAX_WORKERS` | `10` | Max parallel PDF jobs (env: `MAX_WORKERS`) |
| `OPENAI_RATE_LIMIT_RPM` | `450` | OpenAI API rate limit (requests/min). Tier 1: 450, Tier 2: 4500 |
| `TEXT_MODE_MIN_CHARS` | `100` | Min chars for text mode (below = OCR) |
| `GRAPH_PAGE_MAX_CHARS` | `200` | Max chars for graph page detection |
| `OCR_DPI` | `200` | DPI for page rasterisation |
| `OCR_CONFIDENCE_THRESHOLD` | `0.8` | Min OCR confidence to attempt regex. Below this → OpenAI directly |
| `OCR_ENGINE_PRIMARY` | `tesseract` | OCR engine (Tesseract only - PaddleOCR removed) |
| `OCR_ENGINE_FALLBACK` | `tesseract` | Fallback OCR engine (same as primary) |
| `AI_MAX_RETRIES` | `1` | Retry attempts on invalid AI JSON response |

### Performance Tuning for High Volume

**For 300+ docs/day workloads:**

1. **Parallel Processing**: Set `MAX_WORKERS=10` (or higher) in `.env`
   - Processes multiple PDFs simultaneously
   - 10 workers can handle ~300 docs/day comfortably
   - System already implements async parallel processing with proper error isolation

2. **Rate Limiting**: Adjust `OPENAI_RATE_LIMIT_RPM` based on your OpenAI tier
   - Tier 1 (default): 500 RPM → use 450 (safety margin)  
   - Tier 2: 5000 RPM → use 4500  
   - Check your limits: https://platform.openai.com/account/limits
   - Built-in token bucket rate limiter prevents API throttling

3. **Estimated throughput**:
   - Sequential (1 worker): ~80-100 docs/day
   - Parallel (10 workers): ~300-400 docs/day
   - Parallel (15 workers): ~500-600 docs/day (requires Tier 2+)

**Example high-performance `.env`:**
```bash
MAX_WORKERS=12
OPENAI_RATE_LIMIT_RPM=4500  # Tier 2
OCR_CONFIDENCE_THRESHOLD=0.75  # Reduce unnecessary AI calls
```

---

## Security

All data endpoints (`POST /upload`, `GET /status/{id}`, `GET /download/{id}`) require the HTTP header:

```
X-API-Key: <your API_KEY from .env>
```

The server will not start if `API_KEY` or `DATABASE_URL` is unset. The root `/` and `/static` paths are exempt so the browser can load the frontend without authentication.

For production, place the service behind a TLS-terminating reverse proxy (nginx, Caddy) so the key is never transmitted in plaintext.

---

## Confidence Scoring

After all pages are processed, `validator.calculate_confidence()` compares what regex found versus what Claude found for each field:

| Scenario | Confidence | Value used |
|----------|-----------|------------|
| Both sources agree (within 0.1 for numerics) | `HIGH` | Regex value |
| Only regex found the field | `MEDIUM` | Regex value |
| Only Claude found the field | `MEDIUM` | Claude value |
| Both found different values | `LOW` | Stored as `CONFLICT: regex=X claude=Y` |
| Neither found the field | `null` | `null` |

Fields with `LOW` confidence on critical fields are written to Sheet 4 for human review.

---

## Requirements

- Python 3.11+
- Poppler (system dependency for `pdf2image`)
- Tesseract (system dependency for OCR fallback)
- PostgreSQL 14+ (any provider — local Docker, Railway, Render, Supabase, RDS)
- Anthropic API key (dev) or AWS credentials with Bedrock access (production)

---

## Future Roadmap

- **User-scoped auth**: the current `X-API-Key` is service-level; add per-user JWT or session auth for a multi-tenant deployment
- **S3 / object storage**: move Excel BYTEA out of Postgres into S3 for very large batch jobs
- **Patient portal**: extend API with SMS/email delivery endpoints
- **LangGraph multi-agent pipeline**: wrap `ai_extractor.call_ai()` with an agent graph for multi-step reasoning on complex reports
- **Population health dashboard**: query the persistent job store for aggregate analytics across cohorts
- **Production Bedrock**: already supported — flip `ENV=production` in `.env`
- **OCR benchmarking**: run `ocr_extractor` against a ground-truth set of 100 scanned pages to tune `OCR_CONFIDENCE_THRESHOLD` per DC
