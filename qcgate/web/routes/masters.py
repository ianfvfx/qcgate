"""
routes/masters.py — Master detail view and status action endpoints.
"""

import os
import json
import logging
from typing import Optional
from fastapi import APIRouter, Request, Depends, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pathlib import Path

from qcgate.database import get_connection
from qcgate import config
from qcgate.filemover import copy_to_passed, move_to_failed, move_to_passed
from qcgate.proxy import generate_proxy_async
from qcgate.web.routes.auth import require_login

logger = logging.getLogger(__name__)
router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))


def get_master_or_404(master_id: int) -> dict:
    conn = get_connection()
    row = conn.execute("""
        SELECT m.*, j.name AS job_name
        FROM masters m
        JOIN jobs j ON j.id = m.job_id
        WHERE m.id = ?
    """, (master_id,)).fetchone()
    conn.close()
    if not row:
        raise HTTPException(status_code=404, detail="Master not found.")
    return dict(zip(row.keys(), tuple(row)))


# ---------------------------------------------------------------------------
# Detail view
# ---------------------------------------------------------------------------

@router.get("/masters/{master_id}", response_class=HTMLResponse)
async def master_detail(master_id: int, request: Request, user: dict = Depends(require_login)):
    master = get_master_or_404(master_id)

    conn = get_connection()
    iterations = conn.execute("""
        SELECT * FROM iterations WHERE master_id = ? ORDER BY iteration_number DESC
    """, (master_id,)).fetchall()

    presets = conn.execute(
        "SELECT id, name FROM transcode_presets ORDER BY path"
    ).fetchall() if master["status"] == "Passed" else []

    conn.close()

    iterations = [dict(zip(r.keys(), tuple(r))) for r in iterations]
    for it in iterations:
        raw = it.get("qc_flags")
        it["qc_flags_parsed"] = json.loads(raw) if raw else None
    latest_iteration = iterations[0] if iterations else None
    transcoded = request.query_params.get("transcoded")

    return templates.TemplateResponse("master_detail.html", {
        "request": request,
        "user": user,
        "master": master,
        "iterations": iterations,
        "latest_iteration": latest_iteration,
        "presets": [dict(zip(r.keys(), tuple(r))) for r in presets],
        "transcoded": transcoded,
    })


# ---------------------------------------------------------------------------
# Start QC
# ---------------------------------------------------------------------------

@router.post("/masters/{master_id}/start-qc")
async def start_qc(master_id: int, request: Request, user: dict = Depends(require_login)):
    master = get_master_or_404(master_id)

    conn = get_connection()
    conn.execute("""
        UPDATE masters
        SET status = 'QC In Progress', qc_operator = ?, updated_at = datetime('now')
        WHERE id = ?
    """, (user["username"], master_id))

    # Also update the current iteration's status
    conn.execute("""
        UPDATE iterations SET status = 'QC In Progress'
        WHERE master_id = ? AND iteration_number = ?
    """, (master_id, master["current_iteration"]))

    conn.commit()
    conn.close()
    logger.info(f"QC started on master {master_id} by {user['username']}")

    # Redirect back to wherever the user came from
    referer = request.headers.get("referer", "/")
    return RedirectResponse(url=referer, status_code=302)


# ---------------------------------------------------------------------------
# Pass
# ---------------------------------------------------------------------------

@router.post("/masters/{master_id}/pass")
async def pass_master(master_id: int, request: Request, user: dict = Depends(require_login)):
    master = get_master_or_404(master_id)

    if master["status"] not in ("QC In Progress",):
        raise HTTPException(status_code=400, detail="Master must be In Progress to pass.")

    job_name = master["job_name"]
    passed_template = config.get("passed_path")

    # Look up the actual file path stored at ingest time
    conn = get_connection()
    iteration = conn.execute("""
        SELECT file_path FROM iterations
        WHERE master_id = ? AND iteration_number = ?
    """, (master_id, master["current_iteration"])).fetchone()
    conn.close()

    src_path = iteration["file_path"] if iteration and iteration["file_path"] else None

    # Fallback: try forQC folder directly (for masters ingested before file_path was recorded)
    if not src_path or not os.path.exists(src_path):
        watch_path = config.get("watch_path")
        for_qc_dir = watch_path.replace("*", job_name) if "*" in watch_path else watch_path
        fallback = os.path.join(for_qc_dir, master["filename"])
        if os.path.exists(fallback):
            src_path = fallback
        else:
            # Search subfolders of forQC
            for dirpath, dirnames, filenames in os.walk(for_qc_dir):
                dirnames[:] = [d for d in dirnames if not d.startswith(".")]
                if master["filename"] in filenames:
                    src_path = os.path.join(dirpath, master["filename"])
                    break

    if not src_path or not os.path.exists(src_path):
        raise HTTPException(
            status_code=400,
            detail=f"Source file not found for: {master['filename']}"
        )

    # Move from forQC to passed folder
    try:
        published_path = move_to_passed(src_path, passed_template, job_name)
    except Exception as e:
        logger.error(f"Failed to move master {master_id} to passed: {e}")
        raise HTTPException(status_code=500, detail=f"File move failed: {e}")

    conn = get_connection()
    conn.execute("""
        UPDATE masters
        SET status = 'Passed', published_path = ?, updated_at = datetime('now')
        WHERE id = ?
    """, (published_path, master_id))

    conn.execute("""
        UPDATE iterations SET status = 'Passed'
        WHERE master_id = ? AND iteration_number = ?
    """, (master_id, master["current_iteration"]))

    conn.commit()
    conn.close()
    logger.info(f"Master {master_id} passed by {user['username']}, published to {published_path}")

    # Kick off proxy generation in background
    generate_proxy_async(master_id, published_path)

    referer = request.headers.get("referer", "/")
    return RedirectResponse(url=referer, status_code=302)


# ---------------------------------------------------------------------------
# Proxy streaming
# ---------------------------------------------------------------------------

@router.get("/masters/{master_id}/proxy")
async def stream_proxy(master_id: int, request: Request, user: dict = Depends(require_login)):
    """
    Stream the proxy MP4 file to the browser.
    Prefers vault_proxy_path if the master has been vaulted, falls back to proxy_path.
    """
    conn = get_connection()
    row = conn.execute(
        "SELECT proxy_path, vault_proxy_path, proxy_status FROM masters WHERE id = ?",
        (master_id,)
    ).fetchone()
    conn.close()

    if not row or row["proxy_status"] != "ready":
        raise HTTPException(status_code=404, detail="Proxy not available.")

    # Prefer vault copy if it exists
    proxy_path = None
    if row["vault_proxy_path"] and os.path.exists(row["vault_proxy_path"]):
        proxy_path = row["vault_proxy_path"]
    elif row["proxy_path"] and os.path.exists(row["proxy_path"]):
        proxy_path = row["proxy_path"]

    if not proxy_path:
        raise HTTPException(status_code=404, detail="Proxy file not found on disk.")

    from fastapi.responses import FileResponse
    return FileResponse(proxy_path, media_type="video/mp4")
    if not os.path.exists(proxy_path):
        raise HTTPException(status_code=404, detail="Proxy file not found on disk.")

    from fastapi.responses import FileResponse
    return FileResponse(proxy_path, media_type="video/mp4")


# ---------------------------------------------------------------------------
# Fail
# ---------------------------------------------------------------------------

@router.post("/masters/{master_id}/fail")
async def fail_master(
    master_id: int,
    request: Request,
    user: dict = Depends(require_login),
    failure_reason: str = Form(default=""),
):
    master = get_master_or_404(master_id)

    if master["status"] not in ("QC In Progress",):
        raise HTTPException(status_code=400, detail="Master must be In Progress to fail.")

    failed_template = config.get("failed_path")
    job_name = master["job_name"]

    # Look up the actual file path stored at ingest time
    conn = get_connection()
    iteration = conn.execute("""
        SELECT file_path FROM iterations
        WHERE master_id = ? AND iteration_number = ?
    """, (master_id, master["current_iteration"])).fetchone()
    conn.close()

    src_path = iteration["file_path"] if iteration and iteration["file_path"] else None

    # Fallback: search forQC and subfolders
    if not src_path or not os.path.exists(src_path):
        watch_path = config.get("watch_path")
        for_qc_dir = watch_path.replace("*", job_name) if "*" in watch_path else watch_path
        fallback = os.path.join(for_qc_dir, master["filename"])
        if os.path.exists(fallback):
            src_path = fallback
        else:
            for dirpath, dirnames, filenames in os.walk(for_qc_dir):
                dirnames[:] = [d for d in dirnames if not d.startswith(".")]
                if master["filename"] in filenames:
                    src_path = os.path.join(dirpath, master["filename"])
                    break

    if not src_path or not os.path.exists(src_path):
        raise HTTPException(
            status_code=400,
            detail=f"Source file not found for: {master['filename']}"
        )

    try:
        failed_dest = move_to_failed(src_path, failed_template, job_name)
    except Exception as e:
        logger.error(f"Failed to move master {master_id} to failed: {e}")
        raise HTTPException(status_code=500, detail=f"File move failed: {e}")

    conn = get_connection()
    conn.execute("""
        UPDATE masters
        SET status = 'Failed', updated_at = datetime('now')
        WHERE id = ?
    """, (master_id,))

    conn.execute("""
        UPDATE iterations SET status = 'Failed', failure_reason = ?, file_path = ?
        WHERE master_id = ? AND iteration_number = ?
    """, (failure_reason, failed_dest, master_id, master["current_iteration"]))

    conn.commit()
    conn.close()
    logger.info(f"Master {master_id} failed by {user['username']}")

    referer = request.headers.get("referer", "/")
    return RedirectResponse(url=referer, status_code=302)


def _resolve_master_file(master: dict) -> Optional[str]:
    """
    Find the actual file on disk for a master, checking locations in priority order:
      1. iteration file_path (original ingest location, may still be in forQC)
      2. published_path (set after passing)
      3. vault_path (set after vaulting)
    Returns the path string if found, else None.
    """
    conn = get_connection()
    iter_row = conn.execute(
        "SELECT file_path FROM iterations WHERE master_id = ? AND iteration_number = ?",
        (master["id"], master["current_iteration"]),
    ).fetchone()
    conn.close()

    for candidate in [
        iter_row["file_path"] if iter_row else None,
        master.get("published_path"),
        master.get("vault_path"),
    ]:
        if candidate and os.path.exists(candidate):
            return candidate
    return None


@router.post("/masters/{master_id}/refresh-metadata")
async def refresh_metadata(master_id: int, request: Request, user: dict = Depends(require_login)):
    """
    Re-run ffprobe on the current file and update the latest iteration's metadata.
    """
    master = get_master_or_404(master_id)
    ffprobe_path = config.get("ffprobe_path")

    src_path = _resolve_master_file(master)
    if not src_path:
        raise HTTPException(status_code=400, detail=f"File not found for: {master['filename']}")

    from qcgate.ffprobe import extract_metadata
    metadata = extract_metadata(src_path, ffprobe_path)

    conn = get_connection()
    conn.execute("""
        UPDATE iterations
        SET codec = ?, resolution = ?, framerate = ?, duration = ?, audio_channels = ?, scan_type = ?
        WHERE master_id = ? AND iteration_number = ?
    """, (
        metadata.get("codec"),
        metadata.get("resolution"),
        metadata.get("framerate"),
        metadata.get("duration"),
        metadata.get("audio_channels"),
        metadata.get("scan_type"),
        master_id,
        master["current_iteration"],
    ))
    conn.commit()
    conn.close()
    logger.info(f"Metadata refreshed for master {master_id} by {user['username']}")

    return RedirectResponse(url=f"/masters/{master_id}", status_code=302)


@router.post("/masters/{master_id}/update-slate")
async def update_slate(master_id: int, request: Request, user: dict = Depends(require_login)):
    """Save manually edited Version Metadata fields."""
    get_master_or_404(master_id)
    form = await request.form()

    conn = get_connection()
    conn.execute("""
        UPDATE masters
        SET slate_title = ?, slate_version = ?, slate_clock = ?, slate_aspect = ?, slate_duration = ?,
            updated_at = datetime('now')
        WHERE id = ?
    """, (
        form.get("slate_title") or None,
        form.get("slate_version") or None,
        form.get("slate_clock") or None,
        form.get("slate_aspect") or None,
        form.get("slate_duration") or None,
        master_id,
    ))
    conn.commit()
    conn.close()
    logger.info(f"Version Metadata manually updated for master {master_id} by {user['username']}")
    return RedirectResponse(url=f"/masters/{master_id}", status_code=302)


@router.post("/masters/{master_id}/refresh-slate")
async def refresh_slate(master_id: int, request: Request, user: dict = Depends(require_login)):
    """
    Re-run slate OCR on the current iteration's file and update Version Metadata fields.
    """
    master = get_master_or_404(master_id)

    src_path = _resolve_master_file(master)
    if not src_path:
        raise HTTPException(status_code=400, detail=f"File not found for: {master['filename']}")

    from qcgate.slate import extract_slate_metadata
    from qcgate.ffprobe import extract_metadata
    from qcgate.ingest import _format_duration, _derive_title

    ffmpeg_path = config.get("ffmpeg_path")
    tesseract_path = config.get("tesseract_path")
    ffprobe_path = config.get("ffprobe_path")

    tech_meta = extract_metadata(src_path, ffprobe_path)
    slate = extract_slate_metadata(
        src_path,
        ffmpeg_path,
        tesseract_path,
        resolution=tech_meta.get("resolution"),
    )

    if not slate.get("title"):
        stem = os.path.splitext(master["filename"])[0]
        slate["title"] = _derive_title(stem, master["job_name"])
    if not slate.get("duration") and tech_meta.get("duration"):
        slate["duration"] = _format_duration(tech_meta["duration"])

    conn = get_connection()
    conn.execute("""
        UPDATE masters
        SET slate_title = ?, slate_version = ?, slate_clock = ?, slate_aspect = ?, slate_duration = ?,
            updated_at = datetime('now')
        WHERE id = ?
    """, (
        slate.get("title"),
        slate.get("version"),
        slate.get("clock"),
        slate.get("aspect"),
        slate.get("duration"),
        master_id,
    ))
    conn.commit()
    conn.close()
    logger.info(f"Slate metadata refreshed for master {master_id} by {user['username']}")

    return RedirectResponse(url=f"/masters/{master_id}", status_code=302)


# ---------------------------------------------------------------------------
# Rename
# ---------------------------------------------------------------------------

@router.post("/masters/{master_id}/rename")
async def rename_master(
    master_id: int,
    request: Request,
    user: dict = Depends(require_login),
    new_filename: str = Form(...),
):
    master = get_master_or_404(master_id)
    new_filename = new_filename.strip()

    if not new_filename:
        raise HTTPException(status_code=400, detail="Filename cannot be empty.")

    # Check uniqueness within the job
    conn = get_connection()
    conflict = conn.execute("""
        SELECT id FROM masters WHERE job_id = ? AND filename = ? AND id != ?
    """, (master["job_id"], new_filename, master_id)).fetchone()

    if conflict:
        conn.close()
        raise HTTPException(status_code=400, detail="A master with that filename already exists in this job.")

    # Rename the file on disk if it currently exists in for_qc
    watch_path = config.get("watch_path")
    job_name = master["job_name"]
    for_qc_dir = watch_path.replace("*", job_name) if "*" in watch_path else watch_path
    old_path = os.path.join(for_qc_dir, master["filename"])
    new_path = os.path.join(for_qc_dir, new_filename)

    if os.path.exists(old_path):
        try:
            os.rename(old_path, new_path)
            logger.info(f"File renamed on disk: {old_path} -> {new_path}")
        except OSError as e:
            conn.close()
            raise HTTPException(status_code=500, detail=f"File rename failed: {e}")
    else:
        conn.close()
        raise HTTPException(
            status_code=400,
            detail=f"File not found on disk at expected location: {old_path}. Rename cancelled."
        )

    conn.execute("""
        UPDATE masters SET filename = ?, updated_at = datetime('now') WHERE id = ?
    """, (new_filename, master_id))
    conn.commit()
    conn.close()
    logger.info(f"Master {master_id} renamed to '{new_filename}' by {user['username']}")

    return RedirectResponse(url=f"/masters/{master_id}", status_code=302)