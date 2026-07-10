"""
routes/transcode.py — Dispatch passed masters to external encoding watch folders.

Copies a passed master file into a configured transcode preset folder.
The external encoding tool (e.g. Adobe Media Encoder) monitors that folder
and handles the actual encode. QCGate's only job is the copy.
"""

import os
import shutil
import logging
from fastapi import APIRouter, Request, Depends, HTTPException
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from pathlib import Path

from qcgate.database import get_connection
from qcgate.web.routes.auth import require_login

logger = logging.getLogger(__name__)
router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))


@router.post("/masters/{master_id}/transcode/{preset_id}")
async def dispatch_transcode(
    master_id: int,
    preset_id: int,
    request: Request,
    user: dict = Depends(require_login),
):
    """
    Copy a passed master into a transcode preset watch folder.
    """
    conn = get_connection()

    master = conn.execute("""
        SELECT m.*, j.name AS job_name
        FROM masters m JOIN jobs j ON j.id = m.job_id
        WHERE m.id = ?
    """, (master_id,)).fetchone()

    preset = conn.execute(
        "SELECT * FROM transcode_presets WHERE id = ?", (preset_id,)
    ).fetchone()

    conn.close()

    if not master:
        raise HTTPException(status_code=404, detail="Master not found.")
    if not preset:
        raise HTTPException(status_code=404, detail="Preset not found.")

    if master["status"] != "Passed":
        raise HTTPException(status_code=400, detail="Only passed masters can be transcoded.")

    published_path = master["published_path"]
    if not published_path or not os.path.exists(published_path):
        raise HTTPException(
            status_code=400,
            detail=f"Master file not found on disk: {published_path}"
        )

    dest_dir = preset["path"]
    dest_path = os.path.join(dest_dir, os.path.basename(published_path))

    try:
        os.makedirs(dest_dir, exist_ok=True)
        shutil.copy2(published_path, dest_path)
        logger.info(
            f"Dispatched {master['filename']} to preset '{preset['name']}' "
            f"({dest_path}) by {user['username']}"
        )
    except Exception as e:
        logger.error(f"Transcode dispatch failed: {e}")
        raise HTTPException(status_code=500, detail=f"Copy failed: {e}")

    return RedirectResponse(url=f"/masters/{master_id}?transcoded={preset['name']}", status_code=302)
