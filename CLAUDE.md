# QCGate — Claude Project Context

## What This Is

QCGate is a bespoke FastAPI + SQLite web app for managing the QC lifecycle of mastered video deliverables at Black Kite Studios, London. It monitors job folders on network storage, ingests ProRes and MXF masters, tracks them through a TechOps QC workflow, and manages publishing, proxy generation (H264 via ffmpeg), vault archiving, and transcode dispatch.

**Build approach:** Claude builds, Ian tests and reviews. Deliver files by writing them directly to the project. Explain what changed after delivering, not before. Do not make code changes without explicit approval — queries and discussions must remain as discussions until Ian gives a clear go-ahead.

## Environment

- **Project path:** `/Users/ian.fallon/Documents/Claude/qcgate/`
- **Python:** 3.9 — hard constraint, cannot be upgraded
- **Web server:** `uvicorn qcgate.web.app:app --reload --port 8000`
- **Watcher:** `python -m qcgate.watcher`
- **Production server:** Linux, `/opt/qcgate/`, systemd services

## Python Conventions (Hard Rules)

- `Optional[str]` not `str | None`
- `Dict`, `List`, `Tuple` from `typing` — always import from `typing`
- No walrus operator (`:=`)
- No f-string syntax requiring 3.10+

## Critical Dependency Pins

```
bcrypt==4.0.1        # passlib 1.7.4 incompatible with bcrypt 5.x on Python 3.9
passlib==1.7.4
```

Do not upgrade these.

## Key Constraints

- All processing runs locally — no cloud APIs, no external services
- SQLite only — no PostgreSQL
- Network storage via SMB — all file I/O must handle `OSError`/`TimeoutError` gracefully
- `ffmpeg`, `ffprobe`, `tesseract` paths are all configurable in the admin panel (stored in `config` table)
- `datetime('now', 'localtime')` throughout — server runs in BST (UTC+1), SQLite stores local time
- HTTP range requests required for proxy video scrubbing in Chrome — use `_range_response()` in `routes/masters.py`, not `FileResponse`

## Database Migrations

**Never reinitialise the database.** Whenever new columns or config keys are added, always provide a one-off migration snippet:

```bash
python3 - <<'EOF'
import sys
sys.path.insert(0, ".")
from qcgate.database import get_connection
conn = get_connection()
try:
    conn.execute("ALTER TABLE tablename ADD COLUMN colname TYPE")
    print("Added column.")
except Exception as e:
    print(f"Skipped: {e}")
conn.commit()
conn.close()
EOF
```

New config keys must also be added to the `defaults` list in `database.py` for fresh installs, AND provided as a migration for the live DB.

## Config Pattern

All admin-configurable settings live in the `config` table as key/value pairs.

```python
from qcgate import config
value = config.get("key")
config.set("key", "value")
```

Config paths use `*` as the job name placeholder.

## UI Conventions

- Status badge for "QC In Progress" displays as **"QC"** (DB value stays "QC In Progress")
- Delete master: admin-only, red ✕ in a separate column, not in the actions column
- Action buttons in table rows: `flex-wrap:nowrap`
- Dark theme via CSS variables defined in `base.html`
- Modal dialogs for destructive or confirmable actions
- Rename Master and Rename File buttons must not appear for vaulted masters (`vault_path IS NOT NULL`)

## Terminology

| Term | Meaning |
|------|---------|
| Job | Project folder on network storage |
| Master | Individual deliverable file |
| Iteration | Version of a master |
| TechOp | Technical Operations team member |
| Slate / Clock | Title card burned into start of video |
| Proxy | Low-res H264 MP4 preview |
| Vault / mediaVault | Long-term archive storage |
| Transcode preset | Named watch folder for external encoder |

## Known Issues / Tech Debt

- `local_passed_path` config key is redundant (same value as `passed_path`) — low priority cleanup
- Dead code in `routes/masters.py` — unreachable code after a `return` statement
- `qcgate/files/` directory contains apparent duplicate route files (`masters.py`, `stakeholder.py`) — investigate before deleting
- Stakeholder proxy route (`/status/masters/{id}/proxy`) uses `FileResponse` which does not support HTTP range requests — Chrome scrubbing may not work on stakeholder detail pages
- Jobs created by `import_vault.py` have `jobs.path` set to the mediaVault location; vault CSV for those jobs is written there rather than to the live job folder
