"""
routes/stakeholder.py — Read-only status view. No login required.
"""

import os
import logging
from fastapi import APIRouter, Request, HTTPException, Query
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pathlib import Path

from qcgate.database import get_connection

logger = logging.getLogger(__name__)
router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))


PAGE_SIZE = 10


@router.get("/status", response_class=HTMLResponse)
async def stakeholder_dashboard(request: Request, page: int = Query(1, ge=1)):
    conn = get_connection()

    total = conn.execute("SELECT COUNT(*) FROM masters").fetchone()[0]

    offset = (page - 1) * PAGE_SIZE
    masters = conn.execute("""
        SELECT
            m.id, m.filename, m.current_iteration, m.status, m.job_id,
            m.qc_operator,
            j.name AS job_name,
            i.exported_at
        FROM masters m
        JOIN jobs j ON j.id = m.job_id
        LEFT JOIN iterations i
            ON i.master_id = m.id AND i.iteration_number = m.current_iteration
        ORDER BY i.exported_at DESC
        LIMIT ? OFFSET ?
    """, (PAGE_SIZE, offset)).fetchall()
    conn.close()

    total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)

    return templates.TemplateResponse("stakeholder.html", {
        "request": request,
        "user": None,
        "masters": [dict(zip(r.keys(), tuple(r))) for r in masters],
        "page": page,
        "total_pages": total_pages,
        "total": total,
    })


@router.get("/status/masters/{master_id}", response_class=HTMLResponse)
async def stakeholder_master_detail(master_id: int, request: Request):
    conn = get_connection()
    row = conn.execute("""
        SELECT m.*, j.name AS job_name
        FROM masters m JOIN jobs j ON j.id = m.job_id
        WHERE m.id = ?
    """, (master_id,)).fetchone()

    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="Master not found.")

    master = dict(zip(row.keys(), tuple(row)))

    iterations = conn.execute("""
        SELECT * FROM iterations WHERE master_id = ? ORDER BY iteration_number DESC
    """, (master_id,)).fetchall()
    conn.close()

    iterations = [dict(zip(r.keys(), tuple(r))) for r in iterations]

    return templates.TemplateResponse("stakeholder_detail.html", {
        "request": request,
        "user": None,
        "master": master,
        "iterations": iterations,
        "latest_iteration": iterations[0] if iterations else None,
        "proxy_url": f"/status/masters/{master_id}/proxy" if master.get("proxy_status") == "ready" else None,
    })


@router.get("/status/masters/{master_id}/proxy")
async def stakeholder_proxy(master_id: int):
    """Public proxy streaming endpoint for the stakeholder view. No login required."""
    conn = get_connection()
    row = conn.execute(
        "SELECT proxy_path, vault_proxy_path, proxy_status FROM masters WHERE id = ?",
        (master_id,)
    ).fetchone()
    conn.close()

    if not row or row["proxy_status"] != "ready":
        raise HTTPException(status_code=404, detail="Proxy not available.")

    proxy_path = None
    if row["vault_proxy_path"] and os.path.exists(row["vault_proxy_path"]):
        proxy_path = row["vault_proxy_path"]
    elif row["proxy_path"] and os.path.exists(row["proxy_path"]):
        proxy_path = row["proxy_path"]

    if not proxy_path:
        raise HTTPException(status_code=404, detail="Proxy file not found on disk.")

    from fastapi.responses import FileResponse
    return FileResponse(proxy_path, media_type="video/mp4")


@router.get("/status/jobs/{job_id}", response_class=HTMLResponse)
async def stakeholder_job_view(job_id: int, request: Request):
    conn = get_connection()
    job = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if not job:
        conn.close()
        raise HTTPException(status_code=404, detail="Job not found.")

    masters = conn.execute("""
        SELECT m.id, m.filename, m.current_iteration, m.status, m.qc_operator,
               i.exported_at
        FROM masters m
        LEFT JOIN iterations i
            ON i.master_id = m.id AND i.iteration_number = m.current_iteration
        WHERE m.job_id = ?
        ORDER BY i.exported_at DESC
    """, (job_id,)).fetchall()
    conn.close()

    return templates.TemplateResponse("job.html", {
        "request": request,
        "user": None,
        "job": dict(zip(job.keys(), tuple(job))),
        "masters": [dict(zip(r.keys(), tuple(r))) for r in masters],
        "passed_count": 0,
        "vaulted_count": 0,
        "message": None,
        "error": None,
        "base_url": "/status",
    })
