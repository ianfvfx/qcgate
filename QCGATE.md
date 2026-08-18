# QCGate — Technical Handover Document

**Version:** August 2026  
**Author:** Ian Fallon, Technical Operations, Black Kite Studios

---

## Overview

QCGate is a bespoke web application for managing the QC lifecycle of mastered video deliverables. It monitors job folders on network storage, ingests ProRes and MXF masters, tracks them through a TechOps QC workflow, and manages publishing, proxy generation, vault archiving, and transcode dispatch.

The system runs as two persistent processes:

- **Web server** — a FastAPI application served by uvicorn on port 8000
- **Watcher** — a background process that monitors watch folders for new files

---

## Technology Stack

| Component | Technology |
|---|---|
| Language | Python 3.9 (hard constraint — do not upgrade) |
| Web framework | FastAPI 0.115.0 + Uvicorn 0.30.6 |
| Templating | Jinja2 |
| Database | SQLite (WAL mode) |
| Auth | Starlette SessionMiddleware + bcrypt/passlib |
| File monitoring | Watchdog (FSEvents on macOS, inotify on Linux) |
| Video analysis | OpenCV, ffprobe, ffmpeg |
| OCR | Tesseract |

### Critical Dependency Pins

```
bcrypt==4.0.1
passlib==1.7.4
```

These must not be upgraded. passlib 1.7.4 is incompatible with bcrypt 5.x on Python 3.9.

---

## Repository Structure

```
qcgate/
├── .env                        Environment variables
├── requirements.txt            Python dependencies
├── data/
│   └── qcgate.db               SQLite database
├── scripts/
│   ├── init_db.py              One-time DB initialiser + first admin account
│   └── import_vault.py         Bulk import of historical vault assets
└── qcgate/
    ├── config.py               Config table read/write
    ├── database.py             SQLite schema + connection
    ├── ffprobe.py              Technical metadata + loudness extraction
    ├── filemover.py            Move/copy files on pass/fail
    ├── ingest.py               Ingest pipeline (file → DB record)
    ├── proxy.py                H264 proxy generation
    ├── qc_checks.py            Blanking + duplicate frame detection
    ├── slate.py                Slate OCR and metadata parsing
    ├── vault.py                mediaVault archiving + CSV generation
    ├── vault_progress.py       Thread-safe in-memory vault progress state
    ├── watcher.py              Watchdog filesystem monitor
    ├── detection/
    │   └── slate_detector.py   OpenCV-based video analysis
    └── web/
        ├── app.py              FastAPI app + middleware
        ├── templates/          Jinja2 HTML templates
        └── routes/
            ├── admin.py
            ├── auth.py
            ├── conflicts.py
            ├── dashboard.py
            ├── jobs.py
            ├── masters.py
            ├── qc_frames.py
            ├── stakeholder.py
            └── transcode.py
```

---

## Environment Variables

Stored in `.env` at the repo root, loaded at startup via python-dotenv.

| Variable | Default | Purpose |
|---|---|---|
| `SECRET_KEY` | `dev-secret-change-me` | Session cookie signing — **must be changed in production** |
| `DATABASE_PATH` | `data/qcgate.db` | SQLite file path |

All other configuration (paths, tool locations, concurrency) lives in the database `config` table and is managed through the admin panel.

---

## Running the Application

```bash
# Web server
uvicorn qcgate.web.app:app --port 8000

# File watcher (separate process)
python -m qcgate.watcher
```

Both processes should run as systemd services in production. The web server binds to `0.0.0.0:8000` by default. Remove `--reload` in production.

### First-Time Setup

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Initialise database and create first admin account
python3 scripts/init_db.py

# 3. Start both processes
```

---

## Authentication

### How It Works

Authentication uses Starlette's `SessionMiddleware` with signed cookies (`itsdangerous`). The session stores only `user_id`. On every request, the user record is looked up from the database.

- **Login:** `POST /login` — verifies password with bcrypt, sets `session["user_id"]`
- **Logout:** `GET /logout` — clears the session
- **Pre-login redirect:** The intended URL is stored in `session["next"]` and restored after login

### Roles

| Role | Access |
|---|---|
| `techop` | Dashboard, master actions (start QC, pass, fail, rename, transcode), conflict resolution |
| `admin` | All techop access + admin panel (user management, configuration, preset management, master deletion) |

### Route Protection

Two FastAPI dependency functions are used on every protected route:

- `require_login(request)` — redirects to `/login` (HTTP 307) if no valid session
- `require_admin(request)` — additionally checks `role == "admin"`; returns HTTP 403 if not admin

### Stakeholder / Status Pages

The `/status` pages (`/status`, `/status/masters/{id}`, `/status/jobs/{id}`) require **no authentication**. They are read-only and intended for external stakeholders. There is no login prompt.

### Replacing Authentication

If replacing the auth system (e.g. with SSO, LDAP, or a different session mechanism):

1. The dependency functions `require_login` and `require_admin` are in `qcgate/web/routes/auth.py`. Replacing their implementations will propagate to all protected routes automatically — nothing else needs to change.
2. The `users` table and `get_current_user()` function can be retained or replaced depending on the new approach.
3. The session cookie is handled by `SessionMiddleware` in `qcgate/web/app.py`. The `SECRET_KEY` must be a long random string in production.

---

## Configuration

All admin-configurable settings are stored in the `config` database table as key/value strings. They are managed through the admin panel at `/admin`.

| Key | Default | Notes |
|---|---|---|
| `watch_path` | `/jobs/*/mastersExport` | Glob pattern. `*` matches the job folder name |
| `watch_ignore_dirs` | `temp,supplied` | Comma-separated folder names to skip within the watch tree |
| `failed_path` | `/jobs/*/masters/failed` | Destination when a master is failed. `*` replaced with job name |
| `passed_path` | `/jobs/*/masters/passed` | Destination when a master passes |
| `mediavault_path` | `/Volumes/mediaVault` | Root path of the long-term archive |
| `ffmpeg_path` | `/usr/bin/ffmpeg` | Full path to ffmpeg binary |
| `ffprobe_path` | `/usr/bin/ffprobe` | Full path to ffprobe binary |
| `tesseract_path` | `/usr/bin/tesseract` | Full path to Tesseract OCR binary |
| `qc_frames_path` | `/opt/qcgate_qc_frames` | Where QC flag JPEG frames are saved |
| `ingest_concurrency` | `3` | Max concurrent ingest threads. Watcher restart required |
| `qc_scan_concurrency` | `2` | Max concurrent QC scans. Watcher restart required |
| `page_size` | `50` | Masters per page on dashboard and stakeholder views |
| `proxy_concurrency` | `2` | Max concurrent proxy encodes. Web server restart required |

Path values support `*` or `{job}` as the job name placeholder.

---

## Database Schema

SQLite with WAL journal mode and foreign key enforcement. Connection factory: `qcgate/database.py`.

### `jobs`
One record per discovered job folder.

| Column | Type | Notes |
|---|---|---|
| id | INTEGER PK | |
| name | TEXT UNIQUE | Job folder name, e.g. `blackKiteStudios_01234` |
| path | TEXT | Absolute path to job root |
| created_at | TEXT | Local datetime |

### `masters`
One record per unique deliverable filename within a job.

| Column | Type | Notes |
|---|---|---|
| id | INTEGER PK | |
| job_id | INTEGER FK | |
| filename | TEXT | Display name (export timestamp stripped) |
| current_iteration | INTEGER | Increments on resubmission |
| status | TEXT | `Awaiting QC`, `Ingesting`, `QC In Progress`, `Flagged`, `Passed`, `Failed` |
| qc_operator | TEXT | Who started QC |
| published_path | TEXT | Absolute path after passing |
| proxy_status | TEXT | `generating`, `ready`, `failed` |
| proxy_path | TEXT | Path to H264 MP4 proxy |
| vault_path | TEXT | Path to master in mediaVault |
| vault_proxy_path | TEXT | Path to proxy in mediaVault |
| subfolder | TEXT | Subdirectory structure preserved from watch folder |
| slate_title | TEXT | OCR-extracted title |
| slate_version | TEXT | OCR-extracted version |
| slate_clock | TEXT | OCR-extracted clock number |
| slate_aspect | TEXT | OCR-extracted or derived aspect ratio |
| slate_duration | TEXT | OCR-extracted or derived duration |
| created_at / updated_at | TEXT | Local datetimes |

### `iterations`
One record per submitted version of a master.

| Column | Type | Notes |
|---|---|---|
| id | INTEGER PK | |
| master_id | INTEGER FK | |
| iteration_number | INTEGER | Starts at 1 |
| status | TEXT | Status at this version |
| failure_reason | TEXT | If failed |
| exported_at | TEXT | When the file was detected |
| file_path | TEXT | Absolute path at time of ingest |
| codec | TEXT | e.g. `ProRes 422HQ` |
| resolution | TEXT | e.g. `1920x1080` |
| framerate | TEXT | e.g. `25fps` |
| duration | TEXT | HH:MM:SS |
| audio_channels | TEXT | Total channels across all streams |
| scan_type | TEXT | `Progressive` or `Interlaced` |
| loudness | TEXT | e.g. `-23.5 LUFS` |
| qc_flags | TEXT | JSON: blanking segments + duplicate frames |
| qc_scan_status | TEXT | `pending`, `complete`, `failed` |

### `users`

| Column | Type | Notes |
|---|---|---|
| id | INTEGER PK | |
| username | TEXT UNIQUE | |
| password_hash | TEXT | bcrypt |
| role | TEXT | `techop` or `admin` |
| created_at | TEXT | |

### `conflicts`
Files that arrived with a name clash awaiting TechOp decision.

| Column | Type | Notes |
|---|---|---|
| id | INTEGER PK | |
| master_id | INTEGER FK | |
| filepath | TEXT | Path to the conflicting file |
| detected_at | TEXT | |
| resolved | INTEGER | 0 = pending, 1 = resolved |
| resolution | TEXT | `new_iteration` or `discarded` |

### `transcode_presets`

| Column | Type | Notes |
|---|---|---|
| id | INTEGER PK | |
| name | TEXT UNIQUE | Display name |
| path | TEXT | Absolute path to encoding watch folder |
| created_at | TEXT | |

### `config`

| Column | Type |
|---|---|
| key | TEXT PK |
| value | TEXT |
| description | TEXT |

---

## Web Routes Reference

### Public (no authentication)

| Method | URL | Description |
|---|---|---|
| GET | `/login` | Login page |
| POST | `/login` | Submit credentials |
| GET | `/logout` | Clear session |
| GET | `/status` | Stakeholder dashboard (read-only, no login) |
| GET | `/status/masters/{id}` | Stakeholder master detail |
| GET | `/status/masters/{id}/proxy` | Serve proxy video |
| GET | `/status/jobs/{id}` | Stakeholder job view |

### Authenticated (any logged-in user)

| Method | URL | Description |
|---|---|---|
| GET | `/` | Main dashboard. Params: `page`, `q` (search), `filter` |
| GET | `/masters/{id}` | Master detail page |
| POST | `/masters/{id}/start-qc` | Begin QC, set operator |
| POST | `/masters/{id}/pass` | Pass master, move file, generate proxy |
| POST | `/masters/{id}/fail` | Fail master, move file |
| GET | `/masters/{id}/proxy` | Serve proxy (with HTTP range support) |
| POST | `/masters/{id}/refresh-metadata` | Re-run ffprobe |
| POST | `/masters/{id}/update-slate` | Save manually edited slate fields |
| POST | `/masters/{id}/refresh-slate` | Re-run Tesseract OCR |
| POST | `/masters/{id}/rename` | Rename display name only (no disk change) |
| POST | `/masters/{id}/rename-file` | Rename file on disk + update all stored paths |
| POST | `/masters/{id}/transcode/{preset_id}` | Copy master to transcode watch folder |
| GET | `/jobs/{id}` | Job view |
| POST | `/jobs/{id}/vault` | Start vault background job |
| GET | `/jobs/{id}/vault/progress` | SSE stream of vault progress |
| GET | `/conflicts` | Unresolved conflict list |
| POST | `/conflicts/{id}/new-iteration` | Accept conflicting file as new iteration |
| POST | `/conflicts/{id}/discard` | Discard conflicting file |
| GET | `/qc-frames/{filename}` | Serve QC flag JPEG frames |

### Admin only (`/admin/*`)

| Method | URL | Description |
|---|---|---|
| GET | `/admin` | Admin panel |
| POST | `/admin/users/create` | Create user |
| POST | `/admin/users/{id}/set-password` | Change password |
| POST | `/admin/users/{id}/delete` | Delete user |
| POST | `/admin/config` | Save config |
| POST | `/admin/presets/create` | Create transcode preset |
| POST | `/admin/presets/{id}/delete` | Delete transcode preset |
| POST | `/admin/masters/{id}/delete` | Delete master DB record (no file deletion) |

---

## File Watcher

`qcgate/watcher.py` — run as `python -m qcgate.watcher`

**Startup:**
1. Resolves the `watch_path` glob into a list of real directories
2. Pre-populates `seen_files` with all existing files (prevents re-ingest on restart)
3. Starts a watchdog Observer on each directory (recursive)
4. Enters the main loop with a 30-second polling fallback

**Event handling:**
- `on_created` — submits files immediately; for directories, walks and submits all contained files
- `on_moved` — submits new destination if not already tracked; handles dashboard renames without re-ingesting
- `on_deleted` — 3-second deferred removal from `seen_files` to handle SMB spurious delete events

**Ignore logic** — a file is ignored if:
- Filename starts with `.`, `._`, or `.~`
- Extension is not `.mov` or `.mxf`
- Path contains a segment matching `passed_path`, `failed_path`, or `watch_ignore_dirs`

**Linux inotify:** If running on Linux with a large watch tree, the inotify instance and watch limits may need raising:
```bash
sudo sysctl fs.inotify.max_user_watches=524288
sudo sysctl fs.inotify.max_user_instances=512
```
Make permanent by adding to `/etc/sysctl.conf`.

---

## Ingest Pipeline

`qcgate/ingest.py` — triggered by the watcher for each qualifying new file.

1. Strip export timestamp suffix (`_YYYY_MM_DD_HHMM`) from filename to get the canonical master name
2. Skip if extension not `.mov`/`.mxf`
3. Derive job name from the file path using the `watch_path` pattern
4. Derive subfolder (directory structure between watch root and file, timestamps stripped)
5. Get or create job record
6. Run ffprobe to extract technical metadata
7. Measure loudness via ffmpeg ebur128 filter
8. Run Tesseract OCR on the first frame for slate metadata
9. Auto-fail if codec is h264 (wrong codec for a master)
10. Check for existing master with same name:
    - **New file:** create master + iteration 1, run QC checks
    - **Resubmission after failure:** create new iteration, run QC checks
    - **Conflict:** flag for TechOp review on the dashboard

---

## QC Scan

`qcgate/qc_checks.py` + `qcgate/detection/slate_detector.py`

Runs asynchronously after ingest. Requires OpenCV and numpy. Falls back gracefully if unavailable.

**Detects:**
- **Blanking** — persistent black bars at edges (left, right, top, bottom). Multi-threshold analysis (luma 80/50/30/16), spatial consistency, histogram variance. Confidence score: ≥70% = certified (RED), 50–69% = probable (ORANGE). Letterbox bars excluded.
- **Duplicate frames** — consecutive frames with MSE below threshold, confirming neighbours have normal motion.

Results stored as JSON in `iterations.qc_flags`. Annotated JPEG frames saved to `qc_frames_path` for UI display. If issues found, master status set to `Flagged`.

---

## Proxy Generation

`qcgate/proxy.py`

H264 proxy generated automatically when a master is passed.

- **Format:** libx264, main profile, 8 Mbps CBR, AAC 192k, `+faststart` (moov atom at start for web seeking)
- **Output path:** `{passed_dir}/proxies/{stem}_proxy.mp4`
- **Concurrency:** Threading semaphore sized to `proxy_concurrency` config (default 2)
- **Resolution:**
  - 16:9 source → 1280×720
  - 9:16 source → 720×1280
  - 1:1 source → 720×720
  - Other → 50% of source dimensions

Proxies are served with HTTP range request support (for browser scrubbing) at `/masters/{id}/proxy`.

---

## Pass / Fail / Vault Flow

### Pass
1. Master file moved from ingest location to `passed_path`
2. `masters.published_path` updated
3. `masters.status` → `Passed`
4. Proxy generation queued

### Fail
1. Master file moved to `failed_path`
2. `iterations.failure_reason` recorded
3. `masters.status` → `Failed`
4. Master can be resubmitted: next detected file with the same canonical name creates a new iteration

### Vault
1. Admin or TechOp triggers vault from the job view
2. Background thread copies all passed masters (and their proxies) to `{mediavault_path}/{job_name}/{subfolder}/`
3. `vault_path` and `vault_proxy_path` updated in DB
4. A CSV manifest is written to `{job_path}/library/mastersExport/` (where `job_path` is the job's root on the jobs volume, not the mediaVault)
5. Progress is streamed to the browser via SSE
6. Already-vaulted masters are safely skipped (idempotent)

---

## Subfolder Preservation

When a file arrives inside a subdirectory within the watch folder (e.g. `mastersExport/EP01/GRADED/file.mov`), the meaningful subfolder (`EP01/GRADED`) is derived by:

1. Stripping the watch root from the path
2. Removing any path component matching `YYYY_MM_DD` or `YYYY_MM_DD_HHMM` (export timestamp directories)
3. What remains is stored on the master record as `subfolder`

This subfolder is preserved when the file is moved on pass/fail and when it is vaulted, maintaining the original directory organisation.

---

## Vault Import Script

`scripts/import_vault.py` — for bulk import of historical mediaVault assets.

Does not move, rename, or delete any vault files.

```bash
# List all vault jobs with file counts and import status
python3 scripts/import_vault.py --list

# Preview what would be imported (no writes)
python3 scripts/import_vault.py --job JOB_NAME --dry-run

# Import records + generate proxies
python3 scripts/import_vault.py --job JOB_NAME

# Import records only, no proxies
python3 scripts/import_vault.py --job JOB_NAME --no-proxies

# Newest file wins on duplicate master names (useful for loosely organised vaults)
python3 scripts/import_vault.py --job JOB_NAME --reverse
```

Imported masters are created with `status = Passed` and `vault_path = published_path = {file path}`. ffprobe metadata is extracted. Proxies are generated synchronously (respecting `proxy_concurrency`) before the script exits.

---

## Database Migrations

**Never reinitialise the database** — `init_db.py` is for fresh installs only.

For schema changes on a live database, use inline Python:

```bash
cd /opt/qcgate && python3 - <<'EOF'
import sys
sys.path.insert(0, ".")
from qcgate.database import get_connection
conn = get_connection()
try:
    conn.execute("ALTER TABLE masters ADD COLUMN new_column TEXT")
    print("Done.")
except Exception as e:
    print(f"Skipped: {e}")
conn.commit()
conn.close()
EOF
```

New config keys must be added to the `defaults` list in `database.py` for fresh installs, and inserted via migration for the live database:

```bash
cd /opt/qcgate && python3 - <<'EOF'
import sys
sys.path.insert(0, ".")
from qcgate.database import get_connection
conn = get_connection()
conn.execute(
    "INSERT OR IGNORE INTO config (key, value, description) VALUES (?, ?, ?)",
    ("new_key", "default_value", "Description shown in admin panel")
)
conn.commit()
conn.close()
print("Done.")
EOF
```

---

## Database Backup

The database is a single file (`data/qcgate.db`). Safe live backup using Python's built-in SQLite backup API:

```bash
cd /opt/qcgate && python3 - <<'EOF'
import sqlite3
from datetime import date
src = sqlite3.connect("data/qcgate.db")
dst = sqlite3.connect(f"data/qcgate.db.backup_{date.today().strftime('%Y%m%d')}")
src.backup(dst)
dst.close()
src.close()
print("Done.")
EOF
```

---

## Known Issues / Tech Debt

| Issue | Notes |
|---|---|
| `local_passed_path` config key | Redundant — same value as `passed_path`. Low priority to remove |
| Dead code in `routes/masters.py` lines ~208–212 | Unreachable code after a `return` statement |
| `qcgate/files/` directory | Contains apparent duplicate route files (`masters.py`, `stakeholder.py`). Investigate before deleting |
| Starlette `FileResponse` on stakeholder proxy route | Does not support HTTP range requests — scrubbing in Chrome may not work on stakeholder detail pages (works on main pages which use a custom range handler) |
| `aiosqlite` dependency | Imported in requirements but the app uses synchronous SQLite throughout. Can be removed |
