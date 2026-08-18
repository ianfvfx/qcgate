"""
routes/dashboard.py — Main dashboard view.
"""

from fastapi import APIRouter, Request, Depends, Query
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pathlib import Path

from qcgate.database import get_connection
from qcgate import config
from qcgate.web.routes.auth import require_login

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))


def _page_size() -> int:
    try:
        return max(1, int(config.get("page_size") or 50))
    except (ValueError, TypeError):
        return 50


_PENDING_STATUSES = ("Ingesting", "Awaiting QC", "Flagged", "QC In Progress")


@router.get("/", response_class=HTMLResponse)
async def dashboard(
    request: Request,
    user: dict = Depends(require_login),
    page: int = Query(1, ge=1),
    q: str = Query("", alias="q"),
    status_filter: str = Query("", alias="filter"),
):
    page_size = _page_size()
    conn = get_connection()

    search = q.strip()
    sf = status_filter.strip().lower()

    # Build WHERE clauses
    conditions = []
    params = []

    if sf == "pending":
        placeholders = ",".join("?" * len(_PENDING_STATUSES))
        conditions.append(f"m.status IN ({placeholders})")
        params.extend(_PENDING_STATUSES)
    elif sf == "failed":
        conditions.append("m.status = 'Failed'")
    elif sf == "passed":
        conditions.append("m.status = 'Passed'")
    elif sf == "unvaulted":
        conditions.append("m.status = 'Passed' AND m.vault_path IS NULL")

    for word in search.split():
        like = f"%{word}%"
        conditions.append("(m.filename LIKE ? OR j.name LIKE ?)")
        params.extend([like, like])

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

    total = conn.execute(
        f"SELECT COUNT(*) FROM masters m JOIN jobs j ON j.id = m.job_id {where}",
        params
    ).fetchone()[0]

    offset = (page - 1) * page_size
    masters = conn.execute(f"""
        SELECT
            m.id, m.filename, m.current_iteration, m.status,
            m.qc_operator, m.job_id,
            j.name AS job_name,
            i.exported_at
        FROM masters m
        JOIN jobs j ON j.id = m.job_id
        LEFT JOIN iterations i
            ON i.master_id = m.id AND i.iteration_number = m.current_iteration
        {where}
        ORDER BY i.exported_at DESC
        LIMIT ? OFFSET ?
    """, params + [page_size, offset]).fetchall()

    conflicts = conn.execute(
        "SELECT id FROM conflicts WHERE resolved = 0"
    ).fetchall()

    conn.close()

    total_pages = max(1, (total + page_size - 1) // page_size)

    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        "user": user,
        "masters": [dict(zip(r.keys(), tuple(r))) for r in masters],
        "conflicts": conflicts,
        "page": page,
        "total_pages": total_pages,
        "total": total,
        "search": search,
        "status_filter": sf,
    })
