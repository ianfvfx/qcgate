"""
routes/jobs.py — Job drill-down view and vault action.
"""

import asyncio
import json
import logging
import threading
from fastapi import APIRouter, Request, Depends, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from pathlib import Path

from qcgate.database import get_connection
from qcgate.web.routes.auth import require_login
import qcgate.vault_progress as vp

logger = logging.getLogger(__name__)
router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))


@router.get("/jobs/{job_id}", response_class=HTMLResponse)
async def job_view(job_id: int, request: Request, user: dict = Depends(require_login)):
    conn = get_connection()

    job = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if not job:
        conn.close()
        raise HTTPException(status_code=404, detail="Job not found.")

    masters = conn.execute("""
        SELECT
            m.id, m.filename, m.current_iteration, m.status, m.qc_operator,
            m.vault_path,
            i.exported_at
        FROM masters m
        LEFT JOIN iterations i
            ON i.master_id = m.id AND i.iteration_number = m.current_iteration
        WHERE m.job_id = ?
        ORDER BY i.exported_at DESC
    """, (job_id,)).fetchall()
    conn.close()

    masters_list = [dict(zip(r.keys(), tuple(r))) for r in masters]

    # Summary counts for the vault button context
    passed_count = sum(1 for m in masters_list if m["status"] == "Passed")
    vaulted_count = sum(1 for m in masters_list if m.get("vault_path"))

    # If vault just completed, clear progress state so the normal button shows
    state = vp.get(job_id)
    if state and state["status"] == "complete":
        vp.clear(job_id)
        state = None

    return templates.TemplateResponse("job.html", {
        "request": request,
        "user": user,
        "job": dict(zip(job.keys(), tuple(job))),
        "masters": masters_list,
        "passed_count": passed_count,
        "vaulted_count": vaulted_count,
        "vault_running": vp.is_running(job_id),
        "message": request.query_params.get("message"),
        "error": request.query_params.get("error"),
        "base_url": "",
    })


@router.post("/jobs/{job_id}/vault")
async def vault_job_start(job_id: int, request: Request, user: dict = Depends(require_login)):
    """
    Start the vault job in a background thread and redirect immediately.
    Progress is streamed via GET /jobs/{job_id}/vault/progress.
    """
    from qcgate.vault import vault_job as do_vault

    if vp.is_running(job_id):
        return RedirectResponse(url=f"/jobs/{job_id}", status_code=302)

    # Count un-vaulted passed masters
    conn = get_connection()
    total = conn.execute("""
        SELECT COUNT(*) FROM masters
        WHERE job_id = ? AND status = 'Passed' AND vault_path IS NULL
    """, (job_id,)).fetchone()[0]
    conn.close()

    if total == 0:
        return RedirectResponse(url=f"/jobs/{job_id}?message=Nothing+to+vault.", status_code=302)

    vp.start(job_id, total)
    actioned_by = user["username"]

    def run():
        def callback(filename: str, copied: bool, skipped: bool, error: bool, current_only: bool = False) -> None:
            if current_only:
                vp.set_current(job_id, filename)
            else:
                vp.update(job_id, filename, copied=copied, skipped=skipped, error=error)

        try:
            do_vault(job_id, actioned_by, progress_callback=callback)
        except Exception as e:
            logger.error(f"Background vault failed for job {job_id}: {e}")
        finally:
            vp.complete(job_id)

    threading.Thread(target=run, daemon=True).start()
    return RedirectResponse(url=f"/jobs/{job_id}", status_code=302)


@router.get("/jobs/{job_id}/vault/progress")
async def vault_progress_stream(job_id: int, request: Request, user: dict = Depends(require_login)):
    """SSE endpoint — streams vault progress until complete or client disconnects."""

    async def event_stream():
        while True:
            if await request.is_disconnected():
                break
            state = vp.get(job_id)
            if state is None:
                yield f"data: {json.dumps({'status': 'idle'})}\n\n"
                break
            yield f"data: {json.dumps(state)}\n\n"
            if state["status"] == "complete":
                break
            await asyncio.sleep(0.5)

    return StreamingResponse(event_stream(), media_type="text/event-stream")
