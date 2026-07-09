# QCGate — Claude Project Context

## What This Is

QCGate is a bespoke FastAPI + SQLite web app for managing the QC lifecycle of mastered video deliverables at Black Kite Studios, London. It monitors job folders on network storage, ingests ProRes masters, tracks them through a TechOps QC workflow, and manages publishing, proxy generation (H264 via ffmpeg), vault archiving, and transcode dispatch.

**Built by:** Ian Fallon, Head of Technical Operations, Black Kite Studios, London.  
**Build approach:** Claude builds, Ian tests and reviews. Deliver files by writing them directly to the project. Explain what changed after delivering, not before.

## Environment

- **Project path:** `/Users/ian.fallon/Documents/Claude/qcgate/`
- **Python:** 3.9 — hard constraint, cannot be upgraded
- **ffmpeg/ffprobe:** `/opt/homebrew/bin/`
- **Network storage:** `/Volumes/jobs/` (SMB/AFP mount)
- **mediaVault:** `/Volumes/jobs/blackkiteInternal/techops/ian/mediaVault`
- **Web server:** `uvicorn qcgate.web.app:app --reload --port 8000`
- **Watcher:** `python -m qcgate.watcher`

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
- Network storage via SMB/AFP — all file I/O must handle `OSError`/`TimeoutError` gracefully
- `ffmpeg`, `ffprobe`, `tesseract` paths are all configurable in the admin panel (stored in `config` table)

## Database Migrations

**Never reinitialise the database.** The live `data/qcgate.db` contains real test data. Whenever new columns or config keys are added, always provide a one-off migration snippet:

```python
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

Config paths use `*` glob or `{job}` as job name placeholders.

## UI Conventions

- Status badge for "QC In Progress" displays as **"QC"** (DB value stays "QC In Progress")
- Delete master: admin-only, red ✕ in a separate column, not in the actions column
- Action buttons in table rows: `flex-wrap:nowrap`
- Dark theme via CSS variables defined in `base.html`
- Modal dialogs for destructive or confirmable actions

## Terminology

| Term | Meaning |
|------|---------|
| Job | Project folder on network storage |
| Master | Individual deliverable file |
| Iteration | Version of a master |
| TechOp | Technical Operations team member |
| forQC | Watch folder |
| Slate / Clock | Title card burned into start of video |
| Proxy | Low-res H264 MP4 preview |
| Vault / mediaVault | Long-term archive storage |
| Transcode preset | Named watch folder for external encoder |

## Work In Progress

### 1. Slate OCR — COMPLETE

`slate.py` exists and is wired in. Extraction confirmed working on real slates.

- Two-strategy parser: inline (label + value on same line) then columnar (Tesseract two-block layout)
- Aspect ratio normalisation handles Tesseract colon-drop (e.g. `45` → `4:5`)
- Four DB columns added: `slate_title`, `slate_version`, `slate_clock`, `slate_aspect`
- `tesseract_path` config key in admin panel

### 2. Vault Progress Indicator — NOT STARTED

`POST /jobs/{id}/vault` can take minutes for large jobs; the page hangs with no feedback.

Recommended approach: SSE (`GET /jobs/{id}/vault/stream`) — see WIP docs for full spec.

What needs building:
- New route `GET /jobs/{id}/vault/stream` — SSE generator
- Modify `vault.py` to accept a progress callback or yield progress
- Update `job.html` to open EventSource, show progress bar and file list

## Known Issues / Tech Debt

- `local_passed_path` config key is redundant (same value as `passed_path`) — low priority cleanup
- Two stale `published_path` records pointing to old `techOpsTestProject_03991` location
- Dead code in `routes/masters.py` lines 208–212 — unreachable after a `return` statement
- `qcgate/files/` directory contains apparent duplicate route files (`masters.py`, `stakeholder.py`) — investigate before deleting
