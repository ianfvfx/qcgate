"""
auth.py — Login, logout, and session management for QCGate.

Uses signed cookies via Starlette's SessionMiddleware.
Passwords are hashed with bcrypt via passlib.

The `require_login` and `require_admin` dependencies are used
by route handlers to protect pages.
"""

import logging
from typing import Optional

from fastapi import APIRouter, Request, Form, Depends
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from passlib.context import CryptContext
from pathlib import Path

from qcgate.database import get_connection

logger = logging.getLogger(__name__)

router = APIRouter()
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))


# ---------------------------------------------------------------------------
# Session helpers
# ---------------------------------------------------------------------------

def get_current_user(request: Request) -> Optional[dict]:
    """
    Return the current logged-in user dict from the session, or None.
    """
    user_id = request.session.get("user_id")
    if not user_id:
        return None

    conn = get_connection()
    row = conn.execute(
        "SELECT id, username, role FROM users WHERE id = ?", (user_id,)
    ).fetchone()
    conn.close()

    if not row:
        return None

    return {"id": row["id"], "username": row["username"], "role": row["role"]}


def require_login(request: Request) -> dict:
    """
    FastAPI dependency. Redirects to login if not authenticated.
    Use as: user = Depends(require_login)
    """
    user = get_current_user(request)
    if not user:
        # We raise a redirect as an exception so FastAPI handles it cleanly
        from fastapi import HTTPException
        from fastapi.responses import RedirectResponse
        # Store the intended destination so we can redirect after login
        request.session["next"] = str(request.url)
        raise HTTPException(status_code=307, headers={"Location": "/login"})
    return user


def require_admin(request: Request) -> dict:
    """
    FastAPI dependency. Requires admin role.
    """
    user = require_login(request)
    if user["role"] != "admin":
        from fastapi import HTTPException
        raise HTTPException(status_code=403, detail="Admin access required.")
    return user


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    user = get_current_user(request)
    if user:
        return RedirectResponse(url="/", status_code=302)
    return templates.TemplateResponse("login.html", {
        "request": request,
        "error": None
    })


@router.post("/login", response_class=HTMLResponse)
async def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
):
    conn = get_connection()
    row = conn.execute(
        "SELECT id, username, password_hash, role FROM users WHERE username = ?",
        (username,)
    ).fetchone()
    conn.close()

    if not row or not pwd_context.verify(password, row["password_hash"]):
        logger.warning(f"Failed login attempt for username: {username}")
        return templates.TemplateResponse("login.html", {
            "request": request,
            "error": "Invalid username or password."
        })

    request.session["user_id"] = row["id"]
    logger.info(f"User logged in: {row['username']}")

    next_url = request.session.pop("next", "/")
    return RedirectResponse(url=next_url, status_code=302)


@router.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/login", status_code=302)
