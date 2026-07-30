"""
import_vault.py — Import historical mediaVault assets into QCGate.

Walks the mediaVault directory structure and creates Job, Master, and
Iteration records for all qualifying video files without moving, renaming,
or deleting anything.

Usage:
    python3 scripts/import_vault.py --list
        List all top-level job folders in the vault with file counts
        and flag which are already imported.

    python3 scripts/import_vault.py --job JOB_NAME --dry-run
        Show what records would be created without writing anything.

    python3 scripts/import_vault.py --job JOB_NAME
        Import records for one job and queue proxy generation.

    python3 scripts/import_vault.py --job JOB_NAME --no-proxies
        Import records only, skip proxy generation.
"""

import argparse
import logging
import os
import sys
from typing import List, Optional, Tuple

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from qcgate.database import get_connection
from qcgate import config
from qcgate.ingest import strip_export_timestamp, ALLOWED_EXTENSIONS

logger = logging.getLogger(__name__)

IGNORED_PREFIXES = ("._", ".~", ".")
PROXY_DIR_NAME = "proxies"


# ---------------------------------------------------------------------------
# Filesystem helpers
# ---------------------------------------------------------------------------

def _is_ignored_file(filename: str) -> bool:
    """Return True if this file should be skipped."""
    lower = filename.lower()
    for prefix in IGNORED_PREFIXES:
        if lower.startswith(prefix):
            return True
    _, ext = os.path.splitext(lower)
    return ext not in ALLOWED_EXTENSIONS


def _derive_subfolder(file_path: str, job_vault_dir: str) -> Optional[str]:
    """
    Return the subfolder between the job vault root and the file, or None.

    e.g. {vault_root}/MY_JOB/EP01/GRADED/file.mov  ->  EP01/GRADED
         {vault_root}/MY_JOB/file.mov               ->  None
    """
    file_path = os.path.normpath(file_path)
    job_vault_dir = os.path.normpath(job_vault_dir)
    rel = os.path.relpath(os.path.dirname(file_path), job_vault_dir)
    if rel == ".":
        return None
    return rel


def collect_video_files(job_vault_dir: str) -> List[str]:
    """
    Recursively walk the job vault directory and return all qualifying video files.
    Skips hidden files, proxies/ subdirectories, and non-.mov/.mxf files.
    """
    results = []
    for dirpath, dirnames, filenames in os.walk(job_vault_dir):
        # Prune hidden dirs and the proxies output dir in-place
        dirnames[:] = [
            d for d in dirnames
            if not d.startswith(".") and d.lower() != PROXY_DIR_NAME
        ]
        for filename in filenames:
            if _is_ignored_file(filename):
                continue
            results.append(os.path.join(dirpath, filename))
    return sorted(results)


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

def _job_exists(job_name: str) -> Optional[int]:
    """Return the job id if already in the DB, else None."""
    conn = get_connection()
    row = conn.execute(
        "SELECT id FROM jobs WHERE name = ?", (job_name,)
    ).fetchone()
    conn.close()
    return row["id"] if row else None


def _create_job(job_name: str, job_path: str) -> int:
    conn = get_connection()
    conn.execute(
        "INSERT OR IGNORE INTO jobs (name, path) VALUES (?, ?)",
        (job_name, job_path)
    )
    conn.commit()
    row = conn.execute(
        "SELECT id FROM jobs WHERE name = ?", (job_name,)
    ).fetchone()
    job_id = row["id"]
    conn.close()
    return job_id


def _import_master(
    job_id: int,
    master_name: str,
    vault_path: str,
    subfolder: Optional[str],
) -> Tuple[int, bool]:
    """
    Insert master + iteration records.
    Returns (master_id, created) — created=False if already existed.
    """
    conn = get_connection()
    existing = conn.execute(
        "SELECT id FROM masters WHERE job_id = ? AND filename = ?",
        (job_id, master_name)
    ).fetchone()

    if existing:
        conn.close()
        return existing["id"], False

    cursor = conn.execute("""
        INSERT INTO masters
            (job_id, filename, current_iteration, status, subfolder,
             vault_path, published_path)
        VALUES (?, ?, 1, 'Passed', ?, ?, ?)
    """, (job_id, master_name, subfolder, vault_path, vault_path))
    master_id = cursor.lastrowid

    conn.execute("""
        INSERT INTO iterations
            (master_id, iteration_number, status, exported_at, file_path)
        VALUES (?, 1, 'Passed', datetime('now', 'localtime'), ?)
    """, (master_id, vault_path))

    conn.commit()
    conn.close()
    return master_id, True


# ---------------------------------------------------------------------------
# Core import logic
# ---------------------------------------------------------------------------

def import_job(job_name: str, vault_root: str, dry_run: bool, no_proxies: bool) -> None:
    job_vault_dir = os.path.join(vault_root, job_name)
    if not os.path.isdir(job_vault_dir):
        print(f"ERROR: {job_vault_dir} does not exist or is not a directory.")
        sys.exit(1)

    files = collect_video_files(job_vault_dir)
    if not files:
        print(f"No qualifying video files found in {job_vault_dir}")
        return

    print(f"\nJob: {job_name}  ({len(files)} file{'s' if len(files) != 1 else ''})")
    if dry_run:
        print("  [DRY RUN — nothing will be written]\n")

    # Resolve master names upfront for preview
    rows = []
    for file_path in files:
        raw_name = os.path.basename(file_path)
        master_name = strip_export_timestamp(raw_name)
        subfolder = _derive_subfolder(file_path, job_vault_dir)
        rows.append((file_path, master_name, subfolder))

    col_w = max(len(r[1]) for r in rows) + 2
    print(f"  {'Master name':<{col_w}}  {'Subfolder':<30}  Vault path")
    print(f"  {'-' * col_w}  {'-' * 30}  ----------")
    for file_path, master_name, subfolder in rows:
        sf_display = subfolder or "(root)"
        print(f"  {master_name:<{col_w}}  {sf_display:<30}  {file_path}")

    if dry_run:
        return

    print()

    # Create job record
    job_id = _create_job(job_name, job_vault_dir)

    created_count = 0
    skipped_count = 0
    proxy_ids = []

    for file_path, master_name, subfolder in rows:
        master_id, created = _import_master(job_id, master_name, file_path, subfolder)
        if created:
            created_count += 1
            proxy_ids.append((master_id, file_path))
            print(f"  + {master_name}")
        else:
            skipped_count += 1
            print(f"  ~ {master_name}  (already exists, skipped)")

    print(f"\n  Created: {created_count}  Skipped: {skipped_count}")

    if not no_proxies and proxy_ids:
        print(f"  Queuing {len(proxy_ids)} proxy generation tasks...")
        # Import here to avoid loading proxy module until needed
        from qcgate.proxy import generate_proxy_async
        for master_id, file_path in proxy_ids:
            generate_proxy_async(master_id, file_path)
        print(f"  Proxies queued. Generation runs in the background.")
    elif no_proxies:
        print("  Proxy generation skipped (--no-proxies).")

    print()


# ---------------------------------------------------------------------------
# List mode
# ---------------------------------------------------------------------------

def list_jobs(vault_root: str) -> None:
    if not os.path.isdir(vault_root):
        print(f"ERROR: vault root does not exist: {vault_root}")
        sys.exit(1)

    # Fetch job names that have at least one master record
    conn = get_connection()
    imported = {
        row["name"] for row in conn.execute(
            "SELECT DISTINCT j.name FROM jobs j "
            "INNER JOIN masters m ON m.job_id = j.id"
        ).fetchall()
    }
    conn.close()

    entries = sorted([
        e.name for e in os.scandir(vault_root)
        if e.is_dir() and not e.name.startswith(".")
    ])

    if not entries:
        print("No job folders found in vault root.")
        return

    total_files = 0
    print(f"\n{'Job':<50}  {'Files':>6}  Status")
    print(f"{'-' * 50}  {'------':>6}  ------")
    for job_name in entries:
        job_dir = os.path.join(vault_root, job_name)
        files = collect_video_files(job_dir)
        status = "imported" if job_name in imported else "not imported"
        print(f"{job_name:<50}  {len(files):>6}  {status}")
        total_files += len(files)

    print(f"\n  {len(entries)} jobs, {total_files} total video files")
    print()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    logging.basicConfig(
        level=logging.WARNING,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    parser = argparse.ArgumentParser(
        description="Import historical mediaVault assets into QCGate."
    )
    parser.add_argument("--list", action="store_true", help="List all vault jobs with file counts.")
    parser.add_argument("--job", metavar="JOB_NAME", help="Import records for this job.")
    parser.add_argument("--dry-run", action="store_true", help="Preview without writing anything.")
    parser.add_argument("--no-proxies", action="store_true", help="Skip proxy generation.")
    args = parser.parse_args()

    vault_root = config.get("mediavault_path")
    if not vault_root:
        print("ERROR: mediavault_path is not configured in QCGate admin.")
        sys.exit(1)

    if args.list:
        list_jobs(vault_root)
    elif args.job:
        import_job(args.job, vault_root, dry_run=args.dry_run, no_proxies=args.no_proxies)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
