"""
app.py — FastAPI application entry point for QCGate.

Run in development:
    uvicorn qcgate.web.app:app --reload --port 8000

In production this is managed by a systemd service.
"""

import logging
import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from dotenv import load_dotenv

from qcgate.web.routes import dashboard, masters, jobs, admin, auth, stakeholder, conflicts, transcode, qc_frames

load_dotenv()

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------

app = FastAPI(title="QCGate", docs_url=None, redoc_url=None)

# Session middleware — uses SECRET_KEY from .env to sign cookies
app.add_middleware(
    SessionMiddleware,
    secret_key=os.getenv("SECRET_KEY", "dev-secret-change-me"),
    session_cookie="qcgate_session",
    max_age=86400,  # 24 hours
)

# ---------------------------------------------------------------------------
# Static files and templates
# ---------------------------------------------------------------------------

BASE_DIR = Path(__file__).parent

# We'll create a static folder for CSS later
static_dir = BASE_DIR / "static"
static_dir.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

# ---------------------------------------------------------------------------
# Routers
# ---------------------------------------------------------------------------

app.include_router(auth.router)
app.include_router(dashboard.router)
app.include_router(masters.router)
app.include_router(jobs.router)
app.include_router(admin.router)
app.include_router(stakeholder.router)
app.include_router(conflicts.router)
app.include_router(transcode.router)
app.include_router(qc_frames.router)
