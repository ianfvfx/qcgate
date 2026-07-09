"""
routes/dashboard.py — Main dashboard view.
"""

from fastapi import APIRouter, Request, Depends, Query
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pathlib import Path

from qcgate.database import get_connection
from qcgate.web.routes.auth import require_login

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))


PAGE_SIZE = 10


@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, user: dict = Depends(require_login), page: int = Query(1, ge=1)):
    conn = get_connection()

    total = conn.execute("SELECT COUNT(*) FROM masters").fetchone()[0]

    offset = (page - 1) * PAGE_SIZE
    masters = conn.execute("""
        SELECT
            m.id, m.filename, m.current_iteration, m.status,
            m.qc_operator, m.job_id,
            j.name AS job_name,
            i.exported_at
        FROM masters m
        JOIN jobs j ON j.id = m.job_id
        LEFT JOIN iterations i
            ON i.master_id = m.id AND i.iteration_number = m.current_iteration
        ORDER BY i.exported_at DESC
        LIMIT ? OFFSET ?
    """, (PAGE_SIZE, offset)).fetchall()

    conflicts = conn.execute(
        "SELECT id FROM conflicts WHERE resolved = 0"
    ).fetchall()

    conn.close()

    total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)

    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        "user": user,
        "masters": [dict(zip(r.keys(), tuple(r))) for r in masters],
        "conflicts": conflicts,
        "page": page,
        "total_pages": total_pages,
        "total": total,
    })
