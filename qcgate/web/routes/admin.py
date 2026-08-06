"""
routes/admin.py — Admin functions for QCGate.

Covers:
  - User management (create, delete, set password)
  - Path and tool configuration
  - Master record deletion
"""

import logging
from fastapi import APIRouter, Request, Depends, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from passlib.context import CryptContext
from pathlib import Path

from qcgate.database import get_connection
from qcgate import config as cfg
from qcgate.web.routes.auth import require_admin

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin")
templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def get_all_users():
    conn = get_connection()
    rows = conn.execute(
        "SELECT id, username, role, created_at FROM users ORDER BY role, username"
    ).fetchall()
    conn.close()
    return [dict(zip(r.keys(), tuple(r))) for r in rows]


def get_all_presets():
    conn = get_connection()
    rows = conn.execute(
        "SELECT id, name, path, created_at FROM transcode_presets ORDER BY path"
    ).fetchall()
    conn.close()
    return [dict(zip(r.keys(), tuple(r))) for r in rows]


# ---------------------------------------------------------------------------
# Admin home
# ---------------------------------------------------------------------------

@router.get("", response_class=HTMLResponse)
async def admin_home(
    request: Request,
    user: dict = Depends(require_admin),
    message: str = None,
    error: str = None,
):
    return templates.TemplateResponse("admin.html", {
        "request": request,
        "user": user,
        "current_user": user,
        "users": get_all_users(),
        "config": cfg.get_all(),
        "presets": get_all_presets(),
        "message": message,
        "error": error,
    })


# ---------------------------------------------------------------------------
# User management
# ---------------------------------------------------------------------------

@router.post("/users/create")
async def create_user(
    request: Request,
    user: dict = Depends(require_admin),
    username: str = Form(...),
    password: str = Form(...),
    role: str = Form(...),
):
    username = username.strip()
    if not username or not password:
        return RedirectResponse(url="/admin?error=Username+and+password+are+required", status_code=302)

    if role not in ("techop", "admin"):
        return RedirectResponse(url="/admin?error=Invalid+role", status_code=302)

    conn = get_connection()
    existing = conn.execute(
        "SELECT id FROM users WHERE username = ?", (username,)
    ).fetchone()

    if existing:
        conn.close()
        return RedirectResponse(
            url=f"/admin?error=Username+already+exists", status_code=302
        )

    password_hash = pwd_context.hash(password)
    conn.execute(
        "INSERT INTO users (username, password_hash, role) VALUES (?, ?, ?)",
        (username, password_hash, role)
    )
    conn.commit()
    conn.close()
    logger.info(f"User '{username}' ({role}) created by {user['username']}")
    return RedirectResponse(url=f"/admin?message=User+created+successfully", status_code=302)


@router.post("/users/{user_id}/set-password")
async def set_password(
    user_id: int,
    request: Request,
    current_user: dict = Depends(require_admin),
    password: str = Form(...),
):
    if not password:
        return RedirectResponse(url="/admin?error=Password+cannot+be+empty", status_code=302)

    password_hash = pwd_context.hash(password)
    conn = get_connection()
    result = conn.execute(
        "UPDATE users SET password_hash = ? WHERE id = ?", (password_hash, user_id)
    )
    conn.commit()
    conn.close()

    if result.rowcount == 0:
        return RedirectResponse(url="/admin?error=User+not+found", status_code=302)

    logger.info(f"Password updated for user_id={user_id} by {current_user['username']}")
    return RedirectResponse(url="/admin?message=Password+updated", status_code=302)


@router.post("/users/{user_id}/delete")
async def delete_user(
    user_id: int,
    request: Request,
    current_user: dict = Depends(require_admin),
):
    if user_id == current_user["id"]:
        return RedirectResponse(
            url="/admin?error=You+cannot+delete+your+own+account", status_code=302
        )

    conn = get_connection()
    result = conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
    conn.commit()
    conn.close()

    if result.rowcount == 0:
        return RedirectResponse(url="/admin?error=User+not+found", status_code=302)

    logger.info(f"User id={user_id} deleted by {current_user['username']}")
    return RedirectResponse(url="/admin?message=User+deleted", status_code=302)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

@router.post("/config")
async def save_config(request: Request, user: dict = Depends(require_admin)):
    form = await request.form()
    current_keys = cfg.get_all().keys()

    for key in current_keys:
        value = form.get(key)
        if value is not None:
            cfg.set(key, value.strip())
            logger.info(f"Config '{key}' updated by {user['username']}")

    return RedirectResponse(
        url="/admin?message=Configuration+saved.+Restart+the+watcher+to+apply+path+changes.",
        status_code=302
    )


# ---------------------------------------------------------------------------
# Transcode presets
# ---------------------------------------------------------------------------

@router.post("/presets/create")
async def create_preset(
    request: Request,
    user: dict = Depends(require_admin),
    name: str = Form(...),
    path: str = Form(...),
):
    name = name.strip()
    path = path.strip()

    if not name or not path:
        return RedirectResponse(url="/admin?error=Name+and+path+are+required", status_code=302)

    conn = get_connection()
    try:
        conn.execute(
            "INSERT INTO transcode_presets (name, path) VALUES (?, ?)", (name, path)
        )
        conn.commit()
        logger.info(f"Transcode preset '{name}' created by {user['username']}")
    except Exception as e:
        conn.close()
        return RedirectResponse(url="/admin?error=Preset+name+already+exists", status_code=302)
    conn.close()
    return RedirectResponse(url="/admin?message=Preset+created", status_code=302)


@router.post("/presets/{preset_id}/delete")
async def delete_preset(
    preset_id: int,
    request: Request,
    user: dict = Depends(require_admin),
):
    conn = get_connection()
    conn.execute("DELETE FROM transcode_presets WHERE id = ?", (preset_id,))
    conn.commit()
    conn.close()
    logger.info(f"Transcode preset {preset_id} deleted by {user['username']}")
    return RedirectResponse(url="/admin?message=Preset+deleted", status_code=302)


# ---------------------------------------------------------------------------
# Master record deletion
# ---------------------------------------------------------------------------

@router.post("/masters/manual-entry")
async def manual_entry(
    request: Request,
    user: dict = Depends(require_admin),
    job_name: str = Form(...),
    master_name: str = Form(...),
    file_path: str = Form(...),
):
    job_name = job_name.strip()
    master_name = master_name.strip()
    file_path = file_path.strip()

    if not job_name or not master_name or not file_path:
        return RedirectResponse(url="/admin?error=All+fields+are+required", status_code=302)

    conn = get_connection()

    # Get or create job
    conn.execute(
        "INSERT OR IGNORE INTO jobs (name, path) VALUES (?, ?)",
        (job_name, "")
    )
    conn.commit()
    job = conn.execute("SELECT id FROM jobs WHERE name = ?", (job_name,)).fetchone()
    job_id = job["id"]

    # Check for existing master with same name in this job
    existing = conn.execute(
        "SELECT id FROM masters WHERE job_id = ? AND filename = ?",
        (job_id, master_name)
    ).fetchone()
    if existing:
        conn.close()
        return RedirectResponse(
            url="/admin?error=A+master+with+that+name+already+exists+in+this+job",
            status_code=302
        )

    cursor = conn.execute("""
        INSERT INTO masters
            (job_id, filename, current_iteration, status, published_path, vault_path)
        VALUES (?, ?, 1, 'Passed', ?, ?)
    """, (job_id, master_name, file_path, file_path))
    master_id = cursor.lastrowid

    conn.execute("""
        INSERT INTO iterations
            (master_id, iteration_number, status, exported_at, file_path)
        VALUES (?, 1, 'Passed', datetime('now', 'localtime'), ?)
    """, (master_id, file_path))

    conn.commit()
    conn.close()

    logger.info(f"Manual entry created: '{master_name}' in job '{job_name}' by {user['username']}")
    return RedirectResponse(url="/admin?message=Master+record+created", status_code=302)


@router.post("/masters/{master_id}/delete")
async def delete_master(
    master_id: int,
    request: Request,
    user: dict = Depends(require_admin),
):
    form = await request.form()
    redirect_to = form.get("redirect") or "/admin?message=Master+record+deleted"
    conn = get_connection()
    master = conn.execute(
        "SELECT id, filename FROM masters WHERE id = ?", (master_id,)
    ).fetchone()

    if not master:
        conn.close()
        return RedirectResponse(url="/admin?error=Master+not+found", status_code=302)

    # Delete child records first (foreign key constraints)
    conn.execute("DELETE FROM iterations WHERE master_id = ?", (master_id,))
    conn.execute("DELETE FROM conflicts WHERE master_id = ?", (master_id,))
    conn.execute("DELETE FROM masters WHERE id = ?", (master_id,))
    conn.commit()
    conn.close()

    logger.info(
        f"Master record '{master['filename']}' (id={master_id}) deleted by {user['username']}"
    )
    return RedirectResponse(url=redirect_to, status_code=302)
