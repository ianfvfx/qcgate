"""
routes/qc_frames.py — Serve QC flag frame images.

Images are saved to qc_frames_path by qc_checks.py and served here.
Access is restricted to logged-in users.
"""

import os
import logging
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse

from qcgate import config
from qcgate.web.routes.auth import require_login

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/qc-frames/{filename}")
async def serve_qc_frame(filename: str, user: dict = Depends(require_login)):
    frames_dir = config.get("qc_frames_path") or ""
    if not frames_dir:
        raise HTTPException(status_code=404, detail="QC frames directory not configured.")

    # Sanitise — only allow safe filenames (digits, underscores, dot, jpg)
    import re
    if not re.match(r'^[\d_]+\.jpg$', filename):
        raise HTTPException(status_code=400, detail="Invalid filename.")

    path = os.path.join(frames_dir, filename)
    if not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="Frame image not found.")

    return FileResponse(path, media_type="image/jpeg")
