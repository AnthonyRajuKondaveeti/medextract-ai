"""
_db.py

Postgres persistence layer for MedExtract job state.

All SQL lives here. batch_processor calls these functions at key state
transitions — never touches psycopg2 directly.

Schema
------
jobs
    job_id          TEXT PRIMARY KEY
    status          TEXT NOT NULL          -- pending|processing|complete
    excel_bytes     BYTEA
    excel_filename  TEXT
    created_at      TIMESTAMPTZ DEFAULT now()

job_files
    id                      SERIAL PRIMARY KEY
    job_id                  TEXT NOT NULL REFERENCES jobs(job_id)
    filename                TEXT NOT NULL
    status                  TEXT NOT NULL  -- pending|processing|done|failed
    patient_name            TEXT
    error_notes             TEXT
    fields_extracted        INT  DEFAULT 0
    fields_null             INT  DEFAULT 0
    processing_time         REAL DEFAULT 0
    pages_regex_handled     INT  DEFAULT 0
    pages_ocr_handled       INT  DEFAULT 0
    pages_ai_handled    INT  DEFAULT 0
    pages_graph_detected    INT  DEFAULT 0
    unrecovered_fields      TEXT
    total_input_tokens      INT  DEFAULT 0
    total_output_tokens     INT  DEFAULT 0

openai_calls
    id             SERIAL PRIMARY KEY
    job_id         TEXT NOT NULL REFERENCES jobs(job_id)
    filename       TEXT NOT NULL
    page_number    INT
    call_type      TEXT NOT NULL
    input_tokens   INT  DEFAULT 0
    output_tokens  INT  DEFAULT 0
    cost_usd       REAL DEFAULT 0.0
    success        BOOL DEFAULT FALSE
    error_note     TEXT
    created_at     TIMESTAMPTZ DEFAULT now()

Connection
----------
Uses psycopg2.pool.ThreadedConnectionPool — safe for use inside
asyncio.to_thread workers (sync) and from the async FastAPI event loop
(via asyncio.to_thread). Pool size is tuned to MAX_CONCURRENT_EXTRACTIONS.
"""

import logging
from contextlib import contextmanager
from datetime import datetime
from typing import Optional

import psycopg2
import psycopg2.extras
from psycopg2.pool import ThreadedConnectionPool

from config import DATABASE_URL, MAX_CONCURRENT_EXTRACTIONS

logger = logging.getLogger(__name__)

# ── Connection pool ────────────────────────────────────────────────────────────
# minconn=2 keeps warm connections ready.
# maxconn = workers + 4 headroom for status/download endpoint reads and
# concurrent asyncio.to_thread DB calls from multiple file workers.

_pool: Optional[ThreadedConnectionPool] = None


def _get_pool() -> ThreadedConnectionPool:
    global _pool
    if _pool is None:
        raise RuntimeError("Database pool not initialised. Call init_db() first.")
    return _pool


@contextmanager
def _conn():
    """Context manager: borrow a connection from the pool, return it after use."""
    pool = _get_pool()
    conn = pool.getconn()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        pool.putconn(conn)


# ── Schema bootstrap ───────────────────────────────────────────────────────────

def init_db() -> None:
    """
    Create connection pool and ensure schema exists.
    Called once at server startup from main.py lifespan.
    Safe to call on an already-initialised DB (uses CREATE TABLE IF NOT EXISTS).
    """
    global _pool
    _pool = ThreadedConnectionPool(
        minconn=2,
        maxconn=MAX_CONCURRENT_EXTRACTIONS + 4,
        dsn=DATABASE_URL,
    )
    logger.info("Postgres connection pool created.")

    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS jobs (
                    job_id         TEXT PRIMARY KEY,
                    status         TEXT        NOT NULL DEFAULT 'pending',
                    excel_bytes    BYTEA,
                    excel_filename TEXT,
                    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS job_files (
                    id                   SERIAL PRIMARY KEY,
                    job_id               TEXT NOT NULL REFERENCES jobs(job_id),
                    filename             TEXT NOT NULL,
                    status               TEXT NOT NULL DEFAULT 'pending',
                    patient_name         TEXT,
                    error_notes          TEXT,
                    fields_extracted     INT  NOT NULL DEFAULT 0,
                    fields_null          INT  NOT NULL DEFAULT 0,
                    processing_time      REAL NOT NULL DEFAULT 0,
                    pages_regex_handled  INT  NOT NULL DEFAULT 0,
                    pages_ocr_handled    INT  NOT NULL DEFAULT 0,
                    pages_ai_handled INT  NOT NULL DEFAULT 0,
                    pages_graph_detected INT  NOT NULL DEFAULT 0,
                    unrecovered_fields   TEXT,
                    total_input_tokens   INT  NOT NULL DEFAULT 0,
                    total_output_tokens  INT  NOT NULL DEFAULT 0
                )
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_job_files_job_id
                ON job_files(job_id)
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS openai_calls (
                    id             SERIAL PRIMARY KEY,
                    job_id         TEXT NOT NULL REFERENCES jobs(job_id),
                    filename       TEXT NOT NULL,
                    page_number    INT,
                    call_type      TEXT NOT NULL,
                    input_tokens   INT  NOT NULL DEFAULT 0,
                    output_tokens  INT  NOT NULL DEFAULT 0,
                    cost_usd       REAL NOT NULL DEFAULT 0.0,
                    success        BOOL NOT NULL DEFAULT FALSE,
                    error_note     TEXT,
                    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
                )
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_openai_calls_job_id
                ON openai_calls(job_id)
            """)
            # ── Sessions table (PostgreSQL-backed auth sessions) ──────────────
            cur.execute("""
                CREATE TABLE IF NOT EXISTS sessions (
                    token      TEXT PRIMARY KEY,
                    expires_at TIMESTAMPTZ NOT NULL
                )
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_sessions_expires
                ON sessions(expires_at)
            """)
    logger.info("Database schema verified.")


# ── Write operations ───────────────────────────────────────────────────────────

def db_create_job(job_id: str, filenames: list[str]) -> None:
    """Insert a new job row and one job_files row per filename."""
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO jobs (job_id, status) VALUES (%s, 'pending')",
                (job_id,),
            )
            psycopg2.extras.execute_batch(
                cur,
                "INSERT INTO job_files (job_id, filename, status) VALUES (%s, %s, 'pending')",
                [(job_id, fn) for fn in filenames],
            )


def db_set_job_status(job_id: str, status: str) -> None:
    """Update the top-level job status (pending → processing → complete)."""
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE jobs SET status = %s WHERE job_id = %s",
                (status, job_id),
            )


def db_save_excel(job_id: str, excel_bytes: bytes, excel_filename: str) -> None:
    """Persist the generated Excel workbook against the job."""
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE jobs
                SET excel_bytes = %s, excel_filename = %s
                WHERE job_id = %s
                """,
                (psycopg2.Binary(excel_bytes), excel_filename, job_id),
            )


def db_insert_openai_call(
    job_id: str,
    filename: str,
    page_number: Optional[int],
    call_type: str,
    input_tokens: int,
    output_tokens: int,
    cost_usd: float,
    success: bool,
    error_note: Optional[str] = None,
) -> None:
    """Insert one row into openai_calls for every AI API attempt."""
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO openai_calls
                    (job_id, filename, page_number, call_type,
                     input_tokens, output_tokens, cost_usd, success, error_note)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (job_id, filename, page_number, call_type,
                 input_tokens, output_tokens, cost_usd, success, error_note),
            )


def db_update_file(job_id: str, filename: str, file_status) -> None:
    """
    Persist a FileStatus dataclass to job_files after processing completes.
    Called once per file when _process_single_file() finishes.
    """
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE job_files SET
                    status               = %s,
                    patient_name         = %s,
                    error_notes          = %s,
                    fields_extracted     = %s,
                    fields_null          = %s,
                    processing_time      = %s,
                    pages_regex_handled  = %s,
                    pages_ocr_handled    = %s,
                    pages_ai_handled = %s,
                    pages_graph_detected = %s,
                    unrecovered_fields   = %s,
                    total_input_tokens   = %s,
                    total_output_tokens  = %s
                WHERE job_id = %s AND filename = %s
                """,
                (
                    file_status.status,
                    file_status.patient_name,
                    file_status.error_notes,
                    file_status.fields_extracted,
                    file_status.fields_null,
                    file_status.processing_time,
                    file_status.pages_regex_handled,
                    file_status.pages_ocr_handled,
                    file_status.pages_ai_handled,
                    file_status.pages_graph_detected,
                    ", ".join(file_status.unrecovered_fields) if file_status.unrecovered_fields else None,
                    file_status.total_input_tokens,
                    file_status.total_output_tokens,
                    job_id,
                    filename,
                ),
            )


# ── Read operations ────────────────────────────────────────────────────────────

def db_get_job(job_id: str) -> Optional[dict]:
    """
    Return job row as a dict, or None if not found.
    Does NOT include excel_bytes (avoid loading large blobs for status checks).
    """
    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT job_id, status, excel_filename, created_at FROM jobs WHERE job_id = %s",
                (job_id,),
            )
            row = cur.fetchone()
            return dict(row) if row else None


def db_get_excel(job_id: str) -> Optional[bytes]:
    """Return raw Excel bytes for a completed job, or None."""
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT excel_bytes FROM jobs WHERE job_id = %s",
                (job_id,),
            )
            row = cur.fetchone()
            if row and row[0]:
                return bytes(row[0])
            return None


def db_get_files(job_id: str) -> list[dict]:
    """Return all job_files rows for a job as a list of dicts."""
    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT filename, status, patient_name, error_notes,
                       fields_extracted, fields_null, processing_time,
                       pages_regex_handled, pages_ocr_handled,
                       pages_ai_handled, pages_graph_detected,
                       unrecovered_fields,
                       total_input_tokens, total_output_tokens
                FROM job_files
                WHERE job_id = %s
                ORDER BY id
                """,
                (job_id,),
            )
            return [dict(r) for r in cur.fetchall()]


# ── Session CRUD ───────────────────────────────────────────────────────────────

def db_create_session(token: str, expires_at: datetime) -> None:
    """Persist a new login session."""
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO sessions (token, expires_at) VALUES (%s, %s)",
                (token, expires_at),
            )


def db_validate_session(token: str) -> bool:
    """Return True if the token exists and has not expired."""
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM sessions WHERE token = %s AND expires_at > now()",
                (token,),
            )
            return cur.fetchone() is not None


def db_delete_session(token: str) -> None:
    """Remove a session (logout)."""
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM sessions WHERE token = %s", (token,))


def db_cleanup_sessions() -> int:
    """Delete all expired sessions. Returns the number of rows removed."""
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM sessions WHERE expires_at <= now()")
            return cur.rowcount
