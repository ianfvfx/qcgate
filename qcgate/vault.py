"""
vault.py — Copy passed masters and proxies to the mediaVault.

Called when a TechOp triggers the Vault Job action on the job view page.
Copies all passed masters (and their proxies) that have not yet been vaulted
to the mediaVault storage area, then updates the database paths.

Destination structure:
    {mediavault_root}/{job}/filename.mov
    {mediavault_root}/{job}/proxies/filename_proxy.mp4

Safe to run multiple times — already-vaulted masters are skipped.
"""

import csv
import os
import re
import shutil
import logging
from datetime import datetime
from typing import Callable, List, Optional, Tuple

from qcgate.database import get_connection
from qcgate import config

logger = logging.getLogger(__name__)

_JOB_NUMBER_RE = re.compile(r'_\d+$')


def _prog_title(filename, job_name, slate_title):
    """Reconstruct full Prog Title as {job_prefix}_{slate_title}."""
    stem = os.path.splitext(filename)[0]
    if not slate_title:
        return stem
    job_base = _JOB_NUMBER_RE.sub('', job_name)
    if stem.lower().startswith(job_base.lower()):
        job_prefix = stem[:len(job_base)]
        return "{}_{}".format(job_prefix, slate_title)
    return stem


def vault_job(
    job_id: int,
    actioned_by: str,
    progress_callback: Optional[Callable] = None,
) -> Tuple[int, int, list]:
    """
    Copy all un-vaulted passed masters for a job to the mediaVault.

    Returns:
        (copied_count, skipped_count, errors)
        - copied_count: number of masters successfully vaulted
        - skipped_count: number already vaulted or not yet passed
        - errors: list of (filename, error_message) for any failures
    """
    mediavault_root = config.get("mediavault_path")
    if not mediavault_root:
        raise ValueError("mediavault_path is not configured.")

    conn = get_connection()
    job = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if not job:
        conn.close()
        raise ValueError(f"Job {job_id} not found.")

    job_name = job["name"]

    # Vault destination folders
    vault_masters_dir = os.path.join(mediavault_root, job_name)
    vault_proxies_dir = os.path.join(mediavault_root, job_name, "proxies")

    # Get all passed masters for this job
    masters = conn.execute("""
        SELECT id, filename, published_path, proxy_path, proxy_status, vault_path,
               slate_title, slate_clock, slate_duration, slate_aspect, created_at
        FROM masters
        WHERE job_id = ? AND status = 'Passed'
    """, (job_id,)).fetchall()
    conn.close()

    masters = [dict(zip(r.keys(), tuple(r))) for r in masters]

    copied = 0
    skipped = 0
    errors = []
    csv_rows = []  # type: List[dict]

    for master in masters:
        filename = master["filename"]

        # Skip if already vaulted
        if master.get("vault_path"):
            logger.info(f"Skipping already-vaulted master: {filename}")
            skipped += 1
            if progress_callback:
                progress_callback(filename, copied=False, skipped=True, error=False)
            continue

        published_path = master.get("published_path")
        if not published_path or not os.path.exists(published_path):
            logger.warning(f"Master file not found on disk, skipping: {published_path}")
            errors.append((filename, f"Source file not found: {published_path}"))
            if progress_callback:
                progress_callback(filename, copied=False, skipped=False, error=True)
            continue

        if progress_callback:
            progress_callback(filename, copied=False, skipped=False, error=False, current_only=True)

        try:
            # Ensure vault directories exist
            os.makedirs(vault_masters_dir, exist_ok=True)

            # Copy master file
            vault_master_path = os.path.join(vault_masters_dir, filename)
            logger.info(f"Copying master to vault: {published_path} -> {vault_master_path}")
            shutil.copy2(published_path, vault_master_path)

            # Copy proxy if it exists
            vault_proxy_path = None
            proxy_path = master.get("proxy_path")
            if proxy_path and os.path.exists(proxy_path) and master.get("proxy_status") == "ready":
                os.makedirs(vault_proxies_dir, exist_ok=True)
                proxy_filename = os.path.basename(proxy_path)
                vault_proxy_path = os.path.join(vault_proxies_dir, proxy_filename)
                logger.info(f"Copying proxy to vault: {proxy_path} -> {vault_proxy_path}")
                shutil.copy2(proxy_path, vault_proxy_path)

            # Update database with vault paths
            conn = get_connection()
            conn.execute("""
                UPDATE masters
                SET vault_path = ?, vault_proxy_path = ?, updated_at = datetime('now')
                WHERE id = ?
            """, (vault_master_path, vault_proxy_path, master["id"]))
            conn.commit()
            conn.close()

            logger.info(f"Vaulted successfully: {filename}")
            copied += 1
            if progress_callback:
                progress_callback(filename, copied=True, skipped=False, error=False)

            # Accumulate CSV row — only for masters vaulted in this run
            event_date = (master.get("created_at") or "")[:10]
            csv_rows.append({
                "Event Date": event_date,
                "Clocknumber": master.get("slate_clock") or "",
                "Duration": master.get("slate_duration") or "",
                "Prog Title": _prog_title(filename, job_name, master.get("slate_title")),
                "Aspect": master.get("slate_aspect") or "",
                "Event Type": "DIGITAL MASTER",
            })

        except Exception as e:
            logger.error(f"Failed to vault {filename}: {e}")
            errors.append((filename, str(e)))
            if progress_callback:
                progress_callback(filename, copied=False, skipped=False, error=True)

    if csv_rows:
        try:
            job_path = job["path"] or ""
            export_dir = os.path.join(job_path, "library", "mastersExport")
            os.makedirs(export_dir, exist_ok=True)
            today = datetime.now().strftime("%Y_%m_%d")
            csv_filename = "{}_masters_{}.csv".format(job_name, today)
            csv_path = os.path.join(export_dir, csv_filename)
            fieldnames = ["Event Date", "Clocknumber", "Duration", "Prog Title", "Aspect", "Event Type"]
            with open(csv_path, "w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=fieldnames)
                writer.writeheader()
                writer.writerows(csv_rows)
            logger.info(f"Vault CSV written: {csv_path} ({len(csv_rows)} rows)")
        except Exception as e:
            logger.error(f"Failed to write vault CSV: {e}")

    logger.info(
        f"Vault job complete for {job_name} by {actioned_by}: "
        f"{copied} copied, {skipped} skipped, {len(errors)} errors"
    )
    return copied, skipped, errors
