# QCGate

VFX mastering QC middleware for Black Kite Studios. Monitors job folders on network storage, ingests ProRes and MXF masters, tracks them through a TechOps QC workflow, and manages publishing, proxy generation, vault archiving, and transcode dispatch.

## What It Does

- Watches job folders on network storage for new `.mov` and `.mxf` masters
- Ingests files automatically: extracts technical metadata (ffprobe), reads slate via OCR (Tesseract), runs QC checks (blanking detection, duplicate frame detection)
- Tracks masters through a workflow: Awaiting QC → QC In Progress → Passed / Failed / Flagged
- Generates H264 proxy files for web preview
- Archives passed masters to mediaVault with subfolder preservation
- Provides a read-only stakeholder status dashboard (no login required)
- Handles resubmissions (new iterations) and file conflicts

## Quick Start

See **SETUP.md** for full installation instructions.

```bash
# Web server
uvicorn qcgate.web.app:app --port 8000

# File watcher (separate terminal or service)
python -m qcgate.watcher
```

## Project Structure

```
qcgate/
├── .env                        Environment variables (SECRET_KEY, DATABASE_PATH)
├── requirements.txt
├── QCGATE.md                   Full technical reference for the Technology team
├── SETUP.md                    Installation and deployment guide
├── data/
│   └── qcgate.db               SQLite database
├── scripts/
│   ├── init_db.py              One-time database initialiser + first admin account
│   └── import_vault.py         Bulk import of historical mediaVault assets
├── systemd/
│   ├── qcgate-web.service      systemd unit for the web server
│   └── qcgate-watcher.service  systemd unit for the file watcher
└── qcgate/
    ├── config.py               Config table read/write
    ├── database.py             SQLite schema and connection
    ├── ffprobe.py              Technical metadata and loudness extraction
    ├── filemover.py            Move files on pass/fail
    ├── ingest.py               Ingest pipeline
    ├── proxy.py                H264 proxy generation
    ├── qc_checks.py            Blanking and duplicate frame detection
    ├── slate.py                Slate OCR and metadata parsing
    ├── vault.py                mediaVault archiving and CSV generation
    ├── vault_progress.py       Thread-safe vault progress state
    ├── watcher.py              Watchdog filesystem monitor
    ├── detection/
    │   └── slate_detector.py   OpenCV video analysis
    └── web/
        ├── app.py              FastAPI application and middleware
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

## Tech Stack

| Component | Version |
|---|---|
| Python | 3.9 (hard constraint) |
| FastAPI | 0.115.0 |
| Uvicorn | 0.30.6 |
| SQLite | WAL mode |
| Watchdog | 4.0.1 |
| OpenCV | 4.9.0.80 |

**Critical pins — do not upgrade:**
```
bcrypt==4.0.1
passlib==1.7.4
```

## Documentation

- **SETUP.md** — installation, systemd configuration, first-time setup
- **QCGATE.md** — full technical reference: database schema, all routes, config keys, architecture
