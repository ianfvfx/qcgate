"""
filemover.py — Move or copy files between QCGate folders on status change.

Called by the web app when a TechOp sets a master to Passed or Failed.
All file operations are logged. Failures raise exceptions so the caller
can handle them and avoid updating the database if the file move fails.
"""

import shutil
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)


def move_to_failed(src_path: str, failed_path_template: str, job_name: str) -> str:
    """
    Move a file from for_qc to the failed folder.

    Args:
        src_path: Full path to the file currently in for_qc.
        failed_path_template: Config value e.g. /Volumes/jobs/*/masters/failed
                              or with {job} placeholder.
        job_name: The job name derived at ingest (e.g. blackKiteStudios_01234).

    Returns:
        The full destination path the file was moved to.

    Raises:
        FileNotFoundError: If the source file does not exist.
        OSError: If the move operation fails.
    """
    dest_dir = _resolve_path(failed_path_template, job_name)
    dest_path = os.path.join(dest_dir, os.path.basename(src_path))

    _ensure_dir(dest_dir)

    logger.info(f"Moving to failed: {src_path} -> {dest_path}")
    shutil.move(src_path, dest_path)
    logger.info(f"Moved successfully: {dest_path}")

    return dest_path


def copy_to_passed(src_path: str, passed_path_template: str, job_name: str) -> str:
    """
    Copy a file from for_qc to the passed/published folder.
    Kept for reference but move_to_passed is preferred.
    """
    dest_dir = _resolve_path(passed_path_template, job_name)
    dest_path = os.path.join(dest_dir, os.path.basename(src_path))
    _ensure_dir(dest_dir)
    logger.info(f"Copying to passed: {src_path} -> {dest_path}")
    shutil.copy2(src_path, dest_path)
    logger.info(f"Copied successfully: {dest_path}")
    return dest_path


def move_to_passed(src_path: str, passed_path_template: str, job_name: str) -> str:
    """
    Move a file from for_qc to the passed/published folder.
    Preferred over copy_to_passed — files should not remain in for_qc after passing.

    Args:
        src_path: Full path to the file currently in for_qc.
        passed_path_template: Config value with {job} or * placeholder.
        job_name: The job name derived at ingest.

    Returns:
        The full destination path the file was moved to.

    Raises:
        FileNotFoundError: If the source file does not exist.
        OSError: If the move operation fails.
    """
    dest_dir = _resolve_path(passed_path_template, job_name)
    dest_path = os.path.join(dest_dir, os.path.basename(src_path))
    _ensure_dir(dest_dir)
    logger.info(f"Moving to passed: {src_path} -> {dest_path}")
    shutil.move(src_path, dest_path)
    logger.info(f"Moved successfully: {dest_path}")
    return dest_path


def _resolve_path(template: str, job_name: str) -> str:
    """
    Resolve a path template by substituting job name placeholders.

    Handles two placeholder styles:
    - {job}  explicit placeholder e.g. /mnt/delivery/{job}
    - *      glob wildcard used in watch_path style e.g. /Volumes/jobs/*/masters/failed
             In this case * is replaced with the job name.
    """
    if "{job}" in template:
        return template.replace("{job}", job_name)
    if "*" in template:
        return template.replace("*", job_name)
    # No placeholder — return as-is (single fixed destination)
    return template


def _ensure_dir(path: str) -> None:
    """
    Create the destination directory if it does not exist.
    Raises OSError if creation fails.
    """
    Path(path).mkdir(parents=True, exist_ok=True)
