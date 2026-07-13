"""
routes/conflicts.py — Conflict resolution for duplicate filenames.

When a file arrives in forQC with a filename that already exists in the
database for that job (and the existing master is not in Failed status),
a conflict record is created. TechOps must decide what to do with it.

Resolution options:
  - Treat as new iteration: creates a new version of the existing master
  - Discard: removes the conflict record, leaving the existing master unchanged
"""

import os
import logging
from fastapi import APIRouter, Request, Depends, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pathlib import Path

from qcgate.database import get_connection
from qcgate import config
from qcgate.ffprobe import extract_metadata
from qcgate.web.routes.auth import require_login

logger = logging.getLogger(__name__)
router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))


@router.get("/conflicts", response_class=HTMLResponse)
async def conflicts_page(request: Request, user: dict = Depends(require_login)):
    conn = get_connection()
    conflicts = conn.execute("""
        SELECT
            c.id, c.filepath, c.detected_at, c.resolved, c.resolution,
            m.id AS master_id, m.filename, m.status AS master_status,
            m.current_iteration,
            j.name AS job_name
        FROM conflicts c
        JOIN masters m ON m.id = c.master_id
        JOIN jobs j ON j.id = m.job_id
        WHERE c.resolved = 0
        ORDER BY c.detected_at DESC
    """).fetchall()
    conn.close()

    return templates.TemplateResponse("conflicts.html", {
        "request": request,
        "user": user,
        "conflicts": [dict(zip(r.keys(), tuple(r))) for r in conflicts],
    })


@router.post("/conflicts/{conflict_id}/new-iteration")
async def resolve_as_new_iteration(
    conflict_id: int,
    request: Request,
    user: dict = Depends(require_login),
):
    """
    Treat the conflicting file as a new iteration of the existing master.
    Runs ffprobe on the file and creates a new iteration record.
    """
    conn = get_connection()
    conflict = conn.execute(
        "SELECT * FROM conflicts WHERE id = ? AND resolved = 0", (conflict_id,)
    ).fetchone()

    if not conflict:
        conn.close()
        raise HTTPException(status_code=404, detail="Conflict not found or already resolved.")

    conflict = dict(zip(conflict.keys(), tuple(conflict)))
    master_id = conflict["master_id"]
    filepath = conflict["filepath"]

    # Get master and job info
    master = conn.execute("""
        SELECT m.*, j.name AS job_name FROM masters m
        JOIN jobs j ON j.id = m.job_id WHERE m.id = ?
    """, (master_id,)).fetchone()
    master = dict(zip(master.keys(), tuple(master)))
    conn.close()

    if not os.path.exists(filepath):
        raise HTTPException(
            status_code=400,
            detail=f"File no longer exists on disk: {filepath}"
        )

    # Extract metadata and loudness
    ffprobe_path = config.get("ffprobe_path")
    ffmpeg_path = config.get("ffmpeg_path")
    metadata = extract_metadata(filepath, ffprobe_path)
    from qcgate.ffprobe import measure_loudness
    loudness = measure_loudness(filepath, ffmpeg_path)

    # Create new iteration
    conn = get_connection()
    new_iteration = master["current_iteration"] + 1

    conn.execute("""
        UPDATE masters
        SET current_iteration = ?, status = 'Awaiting QC',
            qc_operator = NULL,
            published_path = NULL,
            vault_path = NULL,
            vault_proxy_path = NULL,
            updated_at = datetime('now')
        WHERE id = ?
    """, (new_iteration, master_id))

    conn.execute("""
        INSERT INTO iterations
            (master_id, iteration_number, status, exported_at, file_path,
             codec, resolution, framerate, duration, audio_channels, scan_type, loudness)
        VALUES (?, ?, 'Awaiting QC', datetime('now'), ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        master_id, new_iteration, filepath,
        metadata.get("codec"), metadata.get("resolution"),
        metadata.get("framerate"), metadata.get("duration"),
        metadata.get("audio_channels"), metadata.get("scan_type"),
        loudness,
    ))

    conn.execute("""
        UPDATE conflicts SET resolved = 1, resolution = 'new_iteration'
        WHERE id = ?
    """, (conflict_id,))

    conn.commit()
    conn.close()
    logger.info(
        f"Conflict {conflict_id} resolved as new iteration {new_iteration} "
        f"for master {master_id} by {user['username']}"
    )

    codec = metadata.get("codec") or ""
    if codec.lower() != "h264":
        from qcgate.qc_checks import run_qc_checks_async
        run_qc_checks_async(master_id, new_iteration, filepath)

    return RedirectResponse(url="/conflicts", status_code=302)


@router.post("/conflicts/{conflict_id}/discard")
async def resolve_as_discard(
    conflict_id: int,
    request: Request,
    user: dict = Depends(require_login),
):
    """
    Discard the conflicting file — mark the conflict as resolved with no action.
    The existing master record is left unchanged.
    """
    conn = get_connection()
    conflict = conn.execute(
        "SELECT * FROM conflicts WHERE id = ? AND resolved = 0", (conflict_id,)
    ).fetchone()

    if not conflict:
        conn.close()
        raise HTTPException(status_code=404, detail="Conflict not found or already resolved.")

    conn.execute("""
        UPDATE conflicts SET resolved = 1, resolution = 'discarded'
        WHERE id = ?
    """, (conflict_id,))
    conn.commit()
    conn.close()
    logger.info(f"Conflict {conflict_id} discarded by {user['username']}")
    return RedirectResponse(url="/conflicts", status_code=302)
