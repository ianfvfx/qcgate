"""
vault_progress.py — In-memory progress state for background vault jobs.

Keyed by job_id. The background vault thread writes via update()/complete();
the SSE endpoint reads via get(). Thread-safe via a single lock.
"""

import threading
from typing import Dict, Any, Optional

_lock = threading.Lock()
_state: Dict[int, Dict[str, Any]] = {}


def start(job_id: int, total: int) -> None:
    with _lock:
        _state[job_id] = {
            "status": "running",
            "total": total,
            "done": 0,
            "copied": 0,
            "skipped": 0,
            "error_count": 0,
            "current": None,
        }


def set_current(job_id: int, filename: str) -> None:
    """Mark a file as currently in progress without incrementing the done counter."""
    with _lock:
        p = _state.get(job_id)
        if p:
            p["current"] = filename


def update(job_id: int, current: str, copied: bool, skipped: bool = False, error: bool = False) -> None:
    with _lock:
        p = _state.get(job_id)
        if p is None:
            return
        # Skipped masters were already vaulted — don't count against the total
        if not skipped:
            p["done"] += 1
        p["current"] = current
        if copied:
            p["copied"] += 1
        elif skipped:
            p["skipped"] += 1
        elif error:
            p["error_count"] += 1


def complete(job_id: int) -> None:
    with _lock:
        p = _state.get(job_id)
        if p:
            p["status"] = "complete"
            p["current"] = None


def get(job_id: int) -> Optional[Dict[str, Any]]:
    with _lock:
        p = _state.get(job_id)
        return dict(p) if p else None


def clear(job_id: int) -> None:
    with _lock:
        _state.pop(job_id, None)


def is_running(job_id: int) -> bool:
    with _lock:
        p = _state.get(job_id)
        return p is not None and p["status"] == "running"
