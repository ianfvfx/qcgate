# QCGate

VFX mastering QC middleware for commercial post-production studios.

## Project Structure

```
qcgate/
├── README.md
├── requirements.txt
├── .env.example
├── .gitignore
├── qcgate/
│   ├── __init__.py
│   ├── config.py          # Config read/write (database-backed)
│   ├── database.py        # SQLite connection and schema initialisation
│   ├── models.py          # Data classes / ORM models
│   ├── watcher.py         # Watchdog file watcher
│   ├── ingest.py          # File ingest logic (conflict detection, versioning)
│   ├── ffprobe.py         # ffprobe metadata extraction
│   ├── filemover.py       # File movement logic (for_qc → passed/failed)
│   └── web/
│       ├── __init__.py
│       ├── app.py         # FastAPI application entry point
│       ├── auth.py        # Login / session handling
│       ├── routes/
│       │   ├── __init__.py
│       │   ├── dashboard.py
│       │   ├── masters.py
│       │   ├── jobs.py
│       │   └── admin.py
│       └── templates/     # HTMX/Jinja2 HTML templates
│           ├── base.html
│           ├── dashboard.html
│           ├── job.html
│           ├── master_detail.html
│           ├── login.html
│           └── stakeholder.html
├── scripts/
│   └── init_db.py         # Run once to initialise the database
├── systemd/
│   ├── qcgate-watcher.service
│   └── qcgate-web.service
└── data/
    └── qcgate.db          # SQLite database (created at init)
```

## Setup (Development — Mac or Raspberry Pi)

See SETUP.md for full step-by-step instructions.
