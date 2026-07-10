"""
ingest.py — Handle the ingest of a new file detected by the watcher.

This is the core logic that runs when a file appears in a for_qc folder:
  1. Derive the job name from the file path
  2. Get or create the Job record
  3. Check for filename conflicts
  4. Run ffprobe to extract metadata
  5. Create or update Master and Iteration records
  6. Set status to 'Awaiting QC'
"""

import os
import re
import logging
from datetime import datetime
from typing import Optional, Tuple

from qcgate.database import get_connection
from qcgate import config
from qcgate.ffprobe import extract_metadata, measure_loudness
from qcgate.slate import extract_slate_metadata, extract_clock_from_filename

logger = logging.getLogger(__name__)

# Files with these extensions will be ignored by the watcher
# (temp files, macOS metadata files, etc.)
IGNORED_EXTENSIONS = {".tmp", ".part", ".ds_store", "._"}
IGNORED_PREFIXES = ("._", ".~")

# Matches _YYYY_MM_DD_HHMM export timestamps appended by DCC tools.
# e.g. foo_2025_04_14_1259.mov -> foo.mov
_TIMESTAMP_RE = re.compile(r'_\d{4}_\d{2}_\d{2}_\d{4}$')
# Trailing 6-digit reference/batch codes sometimes appended after EDL tags
_REF_CODE_RE = re.compile(r'_\d{6}$')


def strip_export_timestamp(filename: str) -> str:
    """Remove a trailing _YYYY_MM_DD_HHMM timestamp from a filename stem."""
    stem, ext = os.path.splitext(filename)
    return _TIMESTAMP_RE.sub('', stem) + ext


def _format_duration(hms: str) -> str:
    """
    Convert HH:MM:SS ffprobe duration to a short label.
    Up to 2 minutes: total seconds e.g. '30s', '90s'.
    Over 2 minutes: e.g. '2m30s'.
    """
    try:
        parts = hms.split(":")
        hours = int(parts[0])
        mins = int(parts[1])
        secs = int(parts[2].split(".")[0])
        total = hours * 3600 + mins * 60 + secs
        if total <= 120:
            return f"{total}s"
        m, s = divmod(total, 60)
        return f"{m}m{s:02d}s"
    except (ValueError, IndexError, AttributeError):
        return hms


def _derive_title(filename_stem: str, job_name: str) -> Optional[str]:
    """
    Best-effort title derivation: strip the job name prefix from the filename stem.

    e.g. hondaSuperN_10sec_4x5  (job: hondaSuperN_05106)  -> 10sec_4x5
         UnderArmourHotelArmoure_DG_HOTEL ARMOURE_TRAILER_V6  -> DG_HOTEL_ARMOURE_TRAILER_V6
    """
    # Strip trailing job number (_05106, _05035 etc.) to get the bare project name
    job_base = re.sub(r'_\d+$', '', job_name)

    # Case-insensitive prefix strip
    if not filename_stem.lower().startswith(job_base.lower()):
        return None

    title = filename_stem[len(job_base):]
    title = title.lstrip('_ ')
    title = title.replace(' ', '_')
    title = _REF_CODE_RE.sub('', title)
    title = re.sub(r'_+', '_', title).strip('_')
    return title or None


def should_ignore(filepath: str) -> bool:
    """
    Return True if this file should be silently ignored.
    Covers temp files and macOS metadata files.
    """
    filename = os.path.basename(filepath).lower()

    for prefix in IGNORED_PREFIXES:
        if filename.startswith(prefix):
            return True

    _, ext = os.path.splitext(filename)
    if ext.lower() in IGNORED_EXTENSIONS:
        return True

    return False


def derive_job_name(filepath: str, watch_path: str) -> Optional[str]:
    """
    Derive the job name from the filepath using the configured watch path.

    The job name is the path segment that matched the * in the watch path,
    regardless of how deep within the watch folder the file is located.

    Example (file in subfolder):
        filepath:   /Volumes/jobs/blackKiteStudios_01234/library/mastersExport/forQC/2026-05-12/file.mov
        watch_path: /Volumes/jobs/blackKiteStudios_01234/library/mastersExport/forQC
        -> job_name: blackKiteStudios_01234

    Example (glob path):
        filepath:   /Volumes/jobs/tescoJob_999/masters/for_qc/subdir/file.mov
        watch_path: /Volumes/jobs/*/masters/for_qc
        -> job_name: tescoJob_999
    """
    filepath = os.path.normpath(filepath)
    watch_path = os.path.normpath(watch_path)

    if "*" in watch_path:
        parts = watch_path.split("*")
        prefix = parts[0].rstrip("/")
        suffix = parts[1].lstrip("/") if len(parts) > 1 else ""

        if not filepath.startswith(prefix):
            logger.warning(f"Cannot derive job name: {filepath} does not start with {prefix}")
            return None

        remainder = filepath[len(prefix):].lstrip("/")
        # Job name is always the first segment after the prefix
        job_name = remainder.split("/")[0] if remainder else None
        return job_name

    else:
        # Direct path — job name is derived by walking up from the watch folder
        # The watch folder itself is the forQC dir; we need the job folder above it
        # Structure: /storage/{job}/some/path/forQC/[optional/subfolders/]file.mov
        # We find the watch_path within the filepath and extract the segment before it
        watch_parts = watch_path.split("/")
        file_parts = filepath.split("/")

        # Find where the watch path ends in the file path
        for i in range(len(file_parts) - len(watch_parts) + 1):
            if file_parts[i:i + len(watch_parts)] == watch_parts:
                # Job folder is 3 levels above forQC:
                # forQC -> mastersExport -> library -> job
                watch_start = i
                if watch_start >= 4:
                    return file_parts[watch_start - 3]
                elif watch_start >= 1:
                    return file_parts[watch_start - 1]

        # Fallback: use the folder 3 levels above the forQC folder in the watch path
        parts = watch_path.split("/")
        if len(parts) >= 4:
            return parts[-4]
        return parts[1] if len(parts) > 1 else None


def get_or_create_job(job_name: str, job_root_path: str) -> int:
    """
    Return the job ID for the given job name, creating a record if needed.
    Uses INSERT OR IGNORE to handle concurrent ingest threads racing to create
    the same job simultaneously.
    """
    conn = get_connection()

    conn.execute(
        "INSERT OR IGNORE INTO jobs (name, path) VALUES (?, ?)",
        (job_name, job_root_path)
    )
    conn.commit()

    row = conn.execute(
        "SELECT id FROM jobs WHERE name = ?", (job_name,)
    ).fetchone()

    job_id = row["id"]
    conn.close()
    logger.info(f"Job registered: {job_name} (id={job_id})")
    return job_id


def check_conflict(job_id: int, filename: str) -> Tuple[Optional[int], Optional[str]]:
    """
    Check whether a filename already exists for this job.

    Returns:
        (master_id, current_status) if a conflict exists, else (None, None)
    """
    conn = get_connection()
    row = conn.execute(
        "SELECT id, status FROM masters WHERE job_id = ? AND filename = ?",
        (job_id, filename)
    ).fetchone()
    conn.close()

    if row:
        return row["id"], row["status"]
    return None, None


def record_conflict(master_id: int, filepath: str) -> None:
    """
    Record a conflict in the conflicts table for dashboard display.
    """
    conn = get_connection()
    conn.execute(
        "INSERT INTO conflicts (master_id, filepath) VALUES (?, ?)",
        (master_id, filepath)
    )
    conn.commit()
    conn.close()
    logger.warning(f"Conflict recorded for master_id={master_id}, file={filepath}")


def create_new_iteration(
    master_id: int,
    filepath: str,
    metadata: dict,
    slate: Optional[dict] = None,
) -> int:
    """
    Increment the master's iteration count and create a new iteration record.
    Resets master status to 'Awaiting QC'.

    Returns the new iteration number.
    """
    if slate is None:
        slate = {}

    conn = get_connection()

    # Get current iteration number
    row = conn.execute(
        "SELECT current_iteration FROM masters WHERE id = ?", (master_id,)
    ).fetchone()

    new_iteration = row["current_iteration"] + 1

    # Update master record and overwrite slate fields wholesale.
    # Clear published_path and vault_path — those belong to the previous iteration.
    conn.execute("""
        UPDATE masters
        SET current_iteration = ?,
            status = 'Awaiting QC',
            qc_operator = NULL,
            published_path = NULL,
            vault_path = NULL,
            vault_proxy_path = NULL,
            slate_title = ?,
            slate_version = ?,
            slate_clock = ?,
            slate_aspect = ?,
            slate_duration = ?,
            updated_at = datetime('now')
        WHERE id = ?
    """, (
        new_iteration,
        slate.get("title"),
        slate.get("version"),
        slate.get("clock"),
        slate.get("aspect"),
        slate.get("duration"),
        master_id,
    ))

    # Create iteration record
    conn.execute("""
        INSERT INTO iterations
            (master_id, iteration_number, status, exported_at, file_path,
             codec, resolution, framerate, duration, audio_channels, scan_type, loudness)
        VALUES (?, ?, 'Awaiting QC', datetime('now'), ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        master_id,
        new_iteration,
        filepath,
        metadata.get("codec"),
        metadata.get("resolution"),
        metadata.get("framerate"),
        metadata.get("duration"),
        metadata.get("audio_channels"),
        metadata.get("scan_type"),
        metadata.get("loudness"),
    ))

    conn.commit()
    conn.close()
    logger.info(f"New iteration {new_iteration} created for master_id={master_id}")
    return new_iteration


def ingest_file(filepath: str) -> None:
    """
    Main entry point called by the watcher when a new file is detected.

    Handles the full ingest pipeline:
    - Ignore temp/system files
    - Derive job name
    - Get or create job record
    - Check for conflicts
    - Extract metadata
    - Create or update master and iteration records
    """
    filename = strip_export_timestamp(os.path.basename(filepath))

    if should_ignore(filepath):
        logger.debug(f"Ignoring file: {filepath}")
        return

    logger.info(f"Ingesting file: {filepath} (as '{filename}')")

    # Read current config
    watch_path = config.get("watch_path")
    ffprobe_path = config.get("ffprobe_path")
    ffmpeg_path = config.get("ffmpeg_path")
    tesseract_path = config.get("tesseract_path")

    # Derive job name and path
    job_name = derive_job_name(filepath, watch_path)
    if not job_name:
        logger.error(f"Could not derive job name for: {filepath}")
        return

    # Job root path is the parent of the watch folder
    watch_dir = os.path.dirname(filepath)
    job_root = os.path.normpath(os.path.join(watch_dir, "..", "..", ".."))

    job_id = get_or_create_job(job_name, job_root)

    # Extract technical metadata, loudness, and slate OCR metadata
    metadata = extract_metadata(filepath, ffprobe_path)
    loudness = measure_loudness(filepath, ffmpeg_path)
    slate = extract_slate_metadata(
        filepath,
        ffmpeg_path,
        tesseract_path,
        resolution=metadata.get("resolution"),
    )

    # Fill in any fields the slate didn't provide
    if not slate.get("title"):
        stem = os.path.splitext(filename)[0]
        slate["title"] = _derive_title(stem, job_name)
    if not slate.get("clock"):
        clock_from_filename = extract_clock_from_filename(filename)
        if clock_from_filename:
            slate["clock"] = clock_from_filename
            slate["title"] = clock_from_filename
    if not slate.get("duration") and metadata.get("duration"):
        slate["duration"] = _format_duration(metadata["duration"])
    # aspect fallback is already handled inside extract_slate_metadata

    # Auto-fail h264 — wrong codec for a master deliverable
    codec = metadata.get("codec") or ""
    auto_fail = codec.lower() == "h264"
    auto_fail_reason = "Incorrect Codec" if auto_fail else None
    initial_status = "Failed" if auto_fail else "Awaiting QC"

    # Check for filename conflict
    master_id, current_status = check_conflict(job_id, filename)

    if master_id is None:
        # Brand new file — create master and first iteration
        conn = get_connection()
        cursor = conn.execute("""
            INSERT INTO masters (job_id, filename, current_iteration, status)
            VALUES (?, ?, 1, ?)
        """, (job_id, filename, initial_status))
        master_id = cursor.lastrowid

        conn.execute("""
            INSERT INTO iterations
                (master_id, iteration_number, status, failure_reason, exported_at, file_path,
                 codec, resolution, framerate, duration, audio_channels, scan_type, loudness)
            VALUES (?, 1, ?, ?, datetime('now'), ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            master_id,
            initial_status,
            auto_fail_reason,
            filepath,
            metadata.get("codec"),
            metadata.get("resolution"),
            metadata.get("framerate"),
            metadata.get("duration"),
            metadata.get("audio_channels"),
            metadata.get("scan_type"),
            loudness,
        ))

        conn.execute("""
            UPDATE masters
            SET slate_title = ?, slate_version = ?, slate_clock = ?, slate_aspect = ?, slate_duration = ?
            WHERE id = ?
        """, (
            slate.get("title"),
            slate.get("version"),
            slate.get("clock"),
            slate.get("aspect"),
            slate.get("duration"),
            master_id,
        ))

        conn.commit()
        conn.close()
        if auto_fail:
            logger.warning(f"Auto-failed (h264): {filename} (job={job_name}, master_id={master_id})")
        else:
            logger.info(f"New master created: {filename} (job={job_name}, master_id={master_id})")

    elif current_status == "Failed":
        # Normal resubmission after failure — automatically treat as new iteration
        logger.info(f"Resubmission of failed master: {filename} — creating new iteration")
        create_new_iteration(master_id, filepath, metadata, slate)

    else:
        # Conflict — status is Awaiting QC, QC In Progress, or Passed
        # Flag it on the dashboard for a TechOp to resolve
        logger.warning(
            f"Conflict: {filename} already exists with status '{current_status}' "
            f"in job {job_name}. Flagging for review."
        )
        record_conflict(master_id, filepath)
