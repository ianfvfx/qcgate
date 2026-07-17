"""
database.py — SQLite connection and schema initialisation for QCGate.

All tables are created here. Call initialise_database() once on first run
via scripts/init_db.py before starting any services.
"""

import sqlite3
import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

DATABASE_PATH = os.getenv("DATABASE_PATH", "data/qcgate.db")


def get_connection() -> sqlite3.Connection:
    """
    Return a SQLite connection with row_factory set so results
    can be accessed by column name (e.g. row["filename"]).
    """
    conn = sqlite3.connect(DATABASE_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")   # Better concurrent read/write
    conn.execute("PRAGMA foreign_keys=ON")    # Enforce foreign key constraints
    return conn


def initialise_database() -> None:
    """
    Create all tables and insert default configuration values.
    Safe to call multiple times — uses CREATE TABLE IF NOT EXISTS.
    """
    Path(DATABASE_PATH).parent.mkdir(parents=True, exist_ok=True)

    conn = get_connection()
    cursor = conn.cursor()

    # ------------------------------------------------------------------
    # JOBS
    # One record per discovered job folder on the network.
    # ------------------------------------------------------------------
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS jobs (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            name        TEXT NOT NULL UNIQUE,
            path        TEXT NOT NULL,
            created_at  TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)

    # ------------------------------------------------------------------
    # MASTERS
    # One record per unique filename within a job.
    # Represents the logical deliverable across all its iterations.
    # ------------------------------------------------------------------
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS masters (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id              INTEGER NOT NULL REFERENCES jobs(id),
            filename            TEXT NOT NULL,
            current_iteration   INTEGER NOT NULL DEFAULT 1,
            status              TEXT NOT NULL DEFAULT 'Awaiting QC',
            qc_operator         TEXT,
            published_path      TEXT,
            proxy_status        TEXT,
            proxy_path          TEXT,
            vault_path          TEXT,
            vault_proxy_path    TEXT,
            subfolder           TEXT,
            created_at          TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at          TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE(job_id, filename)
        )
    """)

    # ------------------------------------------------------------------
    # ITERATIONS
    # One record per version of a master file.
    # Each resubmission of the same filename creates a new iteration.
    # ------------------------------------------------------------------
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS iterations (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            master_id        INTEGER NOT NULL REFERENCES masters(id),
            iteration_number INTEGER NOT NULL,
            status           TEXT NOT NULL DEFAULT 'Awaiting QC',
            failure_reason   TEXT,
            exported_at      TEXT NOT NULL DEFAULT (datetime('now')),
            file_path        TEXT,
            codec            TEXT,
            resolution       TEXT,
            framerate        TEXT,
            duration         TEXT,
            audio_channels   TEXT,
            scan_type        TEXT,
            loudness         TEXT,
            qc_flags         TEXT,
            qc_scan_status   TEXT,
            UNIQUE(master_id, iteration_number)
        )
    """)

    # ------------------------------------------------------------------
    # USERS
    # TechOps and admin accounts.
    # ------------------------------------------------------------------
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            username      TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            role          TEXT NOT NULL DEFAULT 'techop',
            created_at    TEXT NOT NULL DEFAULT (datetime('now')),
            CHECK(role IN ('techop', 'admin'))
        )
    """)

    # ------------------------------------------------------------------
    # CONFLICTS
    # Records files that arrived with a conflicting filename and are
    # awaiting a TechOp decision (new iteration vs discard).
    # ------------------------------------------------------------------
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS conflicts (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            master_id   INTEGER NOT NULL REFERENCES masters(id),
            filepath    TEXT NOT NULL,
            detected_at TEXT NOT NULL DEFAULT (datetime('now')),
            resolved    INTEGER NOT NULL DEFAULT 0,
            resolution  TEXT
        )
    """)

    # ------------------------------------------------------------------
    # TRANSCODE_PRESETS
    # Admin-managed list of encoding watch folder destinations.
    # ------------------------------------------------------------------
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS transcode_presets (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            name        TEXT NOT NULL UNIQUE,
            path        TEXT NOT NULL,
            created_at  TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS config (
            key         TEXT PRIMARY KEY,
            value       TEXT NOT NULL,
            description TEXT
        )
    """)

    # Insert default config values (only if they don't already exist)
    defaults = [
        (
            "watch_path",
            "/jobs/*/mastersExport",
            "Glob pattern for incoming files. * is treated as the job name."
        ),
        (
            "failed_path",
            "/jobs/*/masters/failed",
            "Destination when a master is set to Failed. {job} substitution supported."
        ),
        (
            "local_passed_path",
            "/jobs/*/masters/passed",
            "Local archive within the job when a master passes. {job} substitution supported."
        ),
        (
            "passed_path",
            "/jobs/*/masters/passed",
            "Delivery destination when a master passes. May be outside the job folder. {job} substitution supported."
        ),
        (
            "mediavault_path",
            "/Volumes/mediaVault",
            "Root path of the mediaVault long-term archive storage."
        ),
        (
            "ffmpeg_path",
            "/usr/bin/ffmpeg",
            "Full path to the ffmpeg binary on the host system."
        ),
        (
            "ffprobe_path",
            "/usr/bin/ffprobe",
            "Full path to the ffprobe binary on the host system."
        ),
        (
            "tesseract_path",
            "/usr/bin/tesseract",
            "Full path to the Tesseract OCR binary on the host system."
        ),
        (
            "qc_frames_path",
            "/opt/qcgate_qc_frames",
            "Directory where QC flag frame images (JPEGs) are saved."
        ),
        (
            "ingest_concurrency",
            "3",
            "Max files ingested simultaneously (ffprobe + loudness). Requires watcher restart."
        ),
        (
            "qc_scan_concurrency",
            "2",
            "Max QC scans running simultaneously. Requires watcher restart."
        ),
        (
            "page_size",
            "50",
            "Number of masters shown per page on the dashboard and stakeholder views."
        ),
        (
            "proxy_concurrency",
            "2",
            "Max proxy encodes running simultaneously. Requires web server restart."
        ),
    ]

    cursor.executemany("""
        INSERT OR IGNORE INTO config (key, value, description)
        VALUES (?, ?, ?)
    """, defaults)

    conn.commit()
    conn.close()
    print(f"Database initialised at: {DATABASE_PATH}")
