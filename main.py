import logging
import os
import sys
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from typing import Optional

# Ensure src/ is on the path so all module imports resolve
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))

from fastapi import BackgroundTasks, Depends, FastAPI, File, HTTPException, Security, UploadFile, Header
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.security.api_key import APIKeyHeader
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import secrets

from batch_processor import (
    STATUS_COMPLETE,
    create_job,
    get_job,
    get_job_status_payload,
    init_db,
    run_batch,
)
from config import API_KEY, OUTPUT_DIR, UPLOAD_DIR

# ── Logging ────────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


# ── Session Management ─────────────────────────────────────────────────────────
# Simple in-memory session store for login tokens
# For production, use Redis or similar distributed cache

_active_sessions: dict[str, datetime] = {}
SESSION_TIMEOUT_MINUTES = 480  # 8 hours

# Hardcoded admin credentials (in production, use database with hashed passwords)
ADMIN_USERNAME = "admin"
ADMIN_PASSWORD = "Admin@321"


class LoginRequest(BaseModel):
    username: str
    password: str


def create_session_token() -> str:
    """Generate secure random session token."""
    return secrets.token_urlsafe(32)


def validate_session_token(token: Optional[str]) -> bool:
    """Check if session token is valid and not expired."""
    if not token or token not in _active_sessions:
        return False
    
    expiry = _active_sessions[token]
    if datetime.now() > expiry:
        # Remove expired session
        _active_sessions.pop(token, None)
        return False
    
    return True


async def require_session_auth(authorization: Optional[str] = Header(None)) -> None:
    """FastAPI dependency — validates session token from Authorization header."""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=401,
            detail="Authentication required. Please login.",
        )
    
    token = authorization.replace("Bearer ", "")
    if not validate_session_token(token):
        raise HTTPException(
            status_code=401,
            detail="Session expired or invalid. Please login again.",
        )


# ── Authentication ─────────────────────────────────────────────────────────────
# Legacy API key auth (kept for backward compatibility)
# All data endpoints require either session token (preferred) or API key

_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


async def require_api_key(key: str = Security(_api_key_header)) -> None:
    """FastAPI dependency — raises 403 if key is missing or wrong."""
    if not key or key != API_KEY:
        raise HTTPException(
            status_code=403,
            detail="Invalid or missing API key. Provide header: X-API-Key: <key>",
        )


# Combined auth: accepts either session token or API key
async def require_auth(
    authorization: Optional[str] = Header(None),
    api_key: Optional[str] = Security(_api_key_header)
) -> None:
    """Accept either session token (Bearer) or API key."""
    # Try session auth first
    if authorization and authorization.startswith("Bearer "):
        token = authorization.replace("Bearer ", "")
        if validate_session_token(token):
            return
    
    # Fall back to API key
    if api_key and api_key == API_KEY:
        return
    
    raise HTTPException(
        status_code=401,
        detail="Authentication required. Please login or provide valid API key.",
    )


_auth = Depends(require_auth)


# ── App lifecycle ──────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    init_db()   # create Postgres tables if they don't already exist (idempotent)
    logger.info("Medical Extractor API started.")
    yield
    logger.info("Medical Extractor API shutting down.")


app = FastAPI(
    title="Medical Report Extraction Platform",
    description="Extracts structured data from Indian lab PDF reports using OpenAI.",
    version="2.0.0",
    lifespan=lifespan,
)

# Serve static frontend — no auth required so browser can load the UI
app.mount(
    "/static",
    StaticFiles(directory=os.path.join(os.path.dirname(__file__), "static")),
    name="static",
)


# ── Routes ─────────────────────────────────────────────────────────────────────

@app.get("/", include_in_schema=False)
async def serve_index():
    """Serve the frontend SPA — no auth required."""
    index_path = os.path.join(os.path.dirname(__file__), "static", "index.html")
    if not os.path.exists(index_path):
        raise HTTPException(status_code=404, detail="Frontend not found.")
    return FileResponse(index_path)


@app.post("/login")
async def login(credentials: LoginRequest):
    """
    Authenticate with username/password and get session token.
    Returns: { "token": "<session_token>", "expires_in": 28800 }
    """
    if credentials.username != ADMIN_USERNAME or credentials.password != ADMIN_PASSWORD:
        raise HTTPException(status_code=401, detail="Invalid username or password.")
    
    # Create new session
    token = create_session_token()
    expiry = datetime.now() + timedelta(minutes=SESSION_TIMEOUT_MINUTES)
    _active_sessions[token] = expiry
    
    logger.info(f"User '{credentials.username}' logged in successfully.")
    
    return {
        "token": token,
        "expires_in": SESSION_TIMEOUT_MINUTES * 60,  # seconds
        "message": "Login successful"
    }


@app.post("/logout")
async def logout(authorization: Optional[str] = Header(None)):
    """
    Invalidate current session token.
    Requires: Authorization: Bearer <token>
    """
    if authorization and authorization.startswith("Bearer "):
        token = authorization.replace("Bearer ", "")
        _active_sessions.pop(token, None)
        return {"message": "Logged out successfully"}
    
    return {"message": "No active session to logout"}


@app.post("/upload", dependencies=[_auth])
async def upload_pdfs(
    background_tasks: BackgroundTasks,
    files: list[UploadFile] = File(...),
):
    """
    Accept one or more PDF files and start processing.
    Requires header: X-API-Key: <key>
    Returns: { "job_id": "<uuid>" }
    """
    if not files:
        raise HTTPException(status_code=400, detail="No files uploaded.")

    file_payloads: list[tuple[str, bytes]] = []
    for upload in files:
        if not upload.filename:
            raise HTTPException(status_code=400, detail="File missing filename.")
        content = await upload.read()
        if not content:
            raise HTTPException(
                status_code=400,
                detail=f"File '{upload.filename}' is empty.",
            )
        file_payloads.append((upload.filename, content))

    filenames = [fp[0] for fp in file_payloads]
    job_id = create_job(filenames)

    background_tasks.add_task(_run_batch_background, job_id, file_payloads)

    logger.info(f"Job {job_id} queued with {len(file_payloads)} file(s).")
    return JSONResponse(content={"job_id": job_id}, status_code=202)


async def _run_batch_background(
    job_id: str,
    file_payloads: list[tuple[str, bytes]],
) -> None:
    """Background coroutine that drives the batch processor."""
    try:
        await run_batch(job_id, file_payloads)
    except Exception as exc:
        logger.error(f"Job {job_id} background task crashed: {exc}", exc_info=True)


@app.get("/status/{job_id}", dependencies=[_auth])
async def get_status(job_id: str):
    """
    Poll job processing status.
    Requires header: X-API-Key: <key>
    """
    payload = get_job_status_payload(job_id)
    if payload is None:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found.")
    return JSONResponse(content=payload)


@app.get("/download/{job_id}", dependencies=[_auth])
async def download_excel(job_id: str):
    """
    Download the generated Excel report for a completed job.
    Requires header: X-API-Key: <key>
    Only available when job status is 'complete'.
    """
    job = get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found.")

    if job.status != STATUS_COMPLETE:
        raise HTTPException(
            status_code=409,
            detail=f"Job is not complete yet. Current status: '{job.status}'.",
        )

    if not job.excel_bytes:
        raise HTTPException(
            status_code=500,
            detail="Excel file could not be generated for this job.",
        )

    filename = job.excel_filename or "medical_reports.xlsx"

    return Response(
        content=job.excel_bytes,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Content-Length": str(len(job.excel_bytes)),
        },
    )


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=8000,
        reload=False,
        log_level="info",
    )
