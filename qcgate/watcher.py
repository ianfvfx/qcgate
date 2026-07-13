"""
watcher.py — Watchdog-based file watcher for QCGate.

Monitors the configured watch path for new files and triggers
the ingest pipeline when a file is detected.

Run directly for development:
    python -m qcgate.watcher

In production, this runs as a systemd service.
"""

import time
import logging
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler, FileCreatedEvent, FileMovedEvent

from qcgate import config
from qcgate.ingest import ingest_file

logger = logging.getLogger(__name__)

# Maximum number of files that will be ingested concurrently.
# Additional files queue until a slot is free.
INGEST_CONCURRENCY = 3
_ingest_pool = ThreadPoolExecutor(max_workers=INGEST_CONCURRENCY, thread_name_prefix="ingest")

# How long to wait after a file appears before ingesting it.
# Gives the DCC time to finish writing before we read it.
INGEST_DELAY_SECONDS = 5

# How many times to check file size stability before giving up
STABILITY_CHECKS = 6
STABILITY_INTERVAL = 5  # seconds between checks


def wait_for_file_stable(filepath: str) -> bool:
    """
    Wait until a file's size stops changing, indicating it has finished writing.
    Returns True if stable, False if it timed out (caller should ingest anyway).
    """
    last_size = -1
    stable_count = 0

    for _ in range(STABILITY_CHECKS):
        try:
            current_size = os.path.getsize(filepath)
        except OSError:
            return False

        if current_size == last_size and current_size > 0:
            stable_count += 1
            if stable_count >= 2:
                return True
        else:
            stable_count = 0

        last_size = current_size
        time.sleep(STABILITY_INTERVAL)

    logger.warning(
        f"File did not stabilise after {STABILITY_CHECKS * STABILITY_INTERVAL}s "
        f"— ingesting anyway: {filepath}"
    )
    return True  # Ingest regardless; ffprobe may fail and can be re-run from the dashboard


class QCGateEventHandler(FileSystemEventHandler):
    """
    Handles filesystem events from watchdog.
    Responds to both file creation and file move events.
    """

    def __init__(self, seen_files: set, seen_lock: threading.Lock):
        super().__init__()
        self._seen_files = seen_files
        self._seen_lock = seen_lock

    def _handle_file(self, filepath: str) -> None:
        """Common handler for both created and moved-in files."""
        filename = os.path.basename(filepath)
        normalized = os.path.normpath(filepath)

        # Ignore hidden files immediately
        if filename.startswith("."):
            logger.debug(f"Ignoring hidden file: {filepath}")
            return

        # Deduplicate — check and add atomically so that concurrent FSEvents
        # emitter threads and polling threads can't both slip through for the
        # same file.
        with self._seen_lock:
            if normalized in self._seen_files:
                logger.debug(f"Already seen, skipping: {filepath}")
                return
            self._seen_files.add(normalized)

        logger.info(f"File detected: {filepath}")

        # Wait for file to finish writing
        time.sleep(INGEST_DELAY_SECONDS)

        if not os.path.exists(filepath):
            logger.warning(f"File no longer exists after delay, skipping: {filepath}")
            return

        logger.info(f"Waiting for file to finish writing: {filepath}")
        wait_for_file_stable(filepath)

        try:
            ingest_file(filepath)
        except Exception as e:
            logger.error(f"Ingest failed for {filepath}: {e}", exc_info=True)

    def on_created(self, event: FileCreatedEvent) -> None:
        """Fires when a file is written to the watch folder."""
        if event.is_directory:
            return
        _ingest_pool.submit(self._handle_file, event.src_path)

    def on_moved(self, event) -> None:
        """
        Fires when a file is moved. If the destination is inside the watch
        folder, treat it as a new file. If the source was inside the watch
        folder (i.e. moved out), remove it from seen_files so a future
        re-export of the same filename is picked up correctly.
        """
        if event.is_directory:
            return
        src = os.path.normpath(event.src_path)
        dest = os.path.normpath(event.dest_path)
        logger.debug(f"on_moved: {src} -> {dest}")

        with self._seen_lock:
            src_was_seen = src in self._seen_files
            dest_already_seen = dest in self._seen_files
            if src != dest:
                self._seen_files.discard(src)
            # If the source was already tracked, this is a rename of a known file
            # (e.g. a dashboard rename).  Track the new path but don't re-ingest.
            if src_was_seen and not dest_already_seen:
                self._seen_files.add(dest)

        if dest_already_seen or src_was_seen:
            logger.info(f"on_moved: rename of tracked file, updating seen_files only: {src} -> {dest}")
            return

        _ingest_pool.submit(self._handle_file, event.dest_path)

    def on_deleted(self, event) -> None:
        """
        Fires when a file is deleted or moved away by the OS.
        Remove from seen_files so a re-export of the same filename
        is detected correctly.

        On macOS SMB/AFP volumes, spurious delete events fire while a file
        is still being written. Guard with os.path.exists so those don't
        evict a file we're actively tracking.
        """
        if event.is_directory:
            return
        if os.path.exists(event.src_path):
            logger.info(f"on_deleted: file still on disk, ignoring spurious event: {event.src_path}")
            return
        normalized = os.path.normpath(event.src_path)
        with self._seen_lock:
            self._seen_files.discard(normalized)
        logger.info(f"on_deleted: removed from seen_files: {event.src_path}")


def resolve_watch_path(watch_path: str) -> list:
    """
    Resolve the configured watch path into a list of real directory paths to monitor.

    For glob-style paths (containing *), expands to all matching directories.
    For direct paths, returns a single-item list.
    """
    if "*" not in watch_path:
        return [watch_path]

    # Expand glob — split on * and find matching directories
    parts = watch_path.split("*")
    prefix = parts[0].rstrip("/")
    suffix = parts[1].lstrip("/") if len(parts) > 1 else ""

    if not os.path.exists(prefix):
        logger.warning(f"Watch path root does not exist: {prefix}")
        return []

    matches = []
    try:
        for entry in os.scandir(prefix):
            if entry.is_dir():
                candidate = os.path.join(entry.path, suffix) if suffix else entry.path
                if os.path.isdir(candidate):
                    matches.append(candidate)
    except (OSError, TimeoutError) as e:
        logger.warning(f"Could not scan watch path root {prefix} (storage may be unavailable): {e}")
        return []

    if not matches:
        logger.warning(f"No directories matched watch path: {watch_path}")

    return matches


def start_watcher() -> None:
    """
    Start the file watcher. Runs indefinitely until interrupted.

    Uses two complementary strategies:
    1. watchdog FSEvents — low-latency detection of newly written files
    2. Polling fallback — periodic directory scan to catch files moved in
       from other volumes/locations that FSEvents may miss on network storage
    """
    watch_path = config.get("watch_path")
    if not watch_path:
        logger.error("watch_path is not configured. Cannot start watcher.")
        return

    logger.info(f"Resolving watch path: {watch_path}")
    paths_to_watch = resolve_watch_path(watch_path)

    if not paths_to_watch:
        logger.error(f"No valid directories to watch. Check watch_path config: {watch_path}")
        return

    # Track files we've already seen to avoid double-ingesting.
    # seen_lock guards all reads and writes to seen_files — multiple threads
    # (FSEvents emitters and polling threads) access it concurrently.
    seen_files = set()
    seen_lock = threading.Lock()

    # Pre-populate seen_files with anything already in the watch folders
    # so we don't re-ingest on startup
    for path in paths_to_watch:
        try:
            for dirpath, dirnames, filenames in os.walk(path):
                dirnames[:] = [d for d in dirnames if not d.startswith(".")]
                for filename in filenames:
                    if not filename.startswith("."):
                        seen_files.add(os.path.normpath(os.path.join(dirpath, filename)))
        except OSError:
            pass

    observer = Observer()
    handler = QCGateEventHandler(seen_files, seen_lock)

    for path in paths_to_watch:
        observer.schedule(handler, path, recursive=True)
        logger.info(f"Watching (recursive): {path}")

    observer.start()
    logger.info("QCGate watcher started.")

    poll_counter = 0
    POLL_EVERY = 6  # poll every 6 * 5s = 30 seconds

    try:
        while True:
            time.sleep(5)
            poll_counter += 1

            # Re-check for new job folders every 30 seconds (for glob paths)
            if "*" in watch_path:
                try:
                    current_paths = set(resolve_watch_path(watch_path))
                    watched_paths = {str(w.watch.path) for w in observer.emitters}
                    new_paths = current_paths - watched_paths
                    for path in new_paths:
                        observer.schedule(handler, path, recursive=True)
                        logger.info(f"New job folder detected, now watching (recursive): {path}")
                    paths_to_watch = list(current_paths)
                except OSError as e:
                    logger.warning(f"Network scan failed (storage may be unavailable): {e}")

            # Polling fallback — recursively scan directories for files not yet seen
            if poll_counter >= POLL_EVERY:
                poll_counter = 0
                for path in paths_to_watch:
                    try:
                        for dirpath, dirnames, filenames in os.walk(path):
                            # Skip hidden subdirectories
                            dirnames[:] = [d for d in dirnames if not d.startswith(".")]
                            for filename in filenames:
                                if filename.startswith("."):
                                    continue
                                filepath = os.path.normpath(os.path.join(dirpath, filename))
                                with seen_lock:
                                    if filepath in seen_files:
                                        continue
                                logger.info(
                                    f"Polling detected new file (FSEvents missed it): {filepath}"
                                )
                                _ingest_pool.submit(handler._handle_file, filepath)
                    except (OSError, TimeoutError) as e:
                        logger.warning(f"Polling scan error for {path} (storage may be unavailable): {e}")

    except KeyboardInterrupt:
        logger.info("Watcher interrupted by user.")
    finally:
        observer.stop()
        observer.join()
        logger.info("QCGate watcher stopped.")


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    start_watcher()
