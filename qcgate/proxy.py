"""
proxy.py — Generate H264 MP4 proxy files for passed masters.

Called in a background thread when a master is set to Passed.
The proxy is stored alongside the master in the passed folder.

Resolution rules:
  16:9  -> 1280x720
  9:16  -> 720x1280
  1:1   -> 720x720
  other -> 50% of original resolution (rounded to even numbers)

Bitrate: 8Mbps video, 192k AAC audio.
"""

import subprocess
import threading
import logging
import os
from typing import Optional

from qcgate.database import get_connection
from qcgate import config

logger = logging.getLogger(__name__)

PROXY_BITRATE = "8M"
AUDIO_BITRATE = "192k"


def _aspect_ratio(width: int, height: int) -> str:
    """Return a simplified aspect ratio string."""
    from math import gcd
    g = gcd(width, height)
    return f"{width // g}:{height // g}"


def _target_resolution(width: int, height: int):
    """
    Return (target_width, target_height) based on aspect ratio rules.
    All values rounded to nearest even number (required by H264).
    """
    ratio = _aspect_ratio(width, height)

    if ratio == "16:9":
        return 1280, 720
    elif ratio == "9:16":
        return 720, 1280
    elif ratio == "1:1":
        return 720, 720
    else:
        # 50% resolution, rounded to even
        tw = int(width * 0.5)
        th = int(height * 0.5)
        tw = tw if tw % 2 == 0 else tw + 1
        th = th if th % 2 == 0 else th + 1
        return tw, th


def _proxy_path(source_path: str) -> str:
    """
    Return the proxy file path for a given source file.
    Proxy lives in a proxies/ subfolder next to the passed file.
    e.g. .../passed/myfile.mov -> .../passed/proxies/myfile_proxy.mp4
    """
    passed_dir = os.path.dirname(source_path)
    proxy_dir = os.path.join(passed_dir, "proxies")
    os.makedirs(proxy_dir, exist_ok=True)
    basename = os.path.splitext(os.path.basename(source_path))[0]
    return os.path.join(proxy_dir, f"{basename}_proxy.mp4")


def generate_proxy(master_id: int, source_path: str) -> None:
    """
    Generate a proxy for the given source file and update the master record.
    Intended to be run in a background thread.
    """
    ffmpeg_path = config.get("ffmpeg_path") or "ffmpeg"
    ffprobe_path = config.get("ffprobe_path") or "ffprobe"

    logger.info(f"Starting proxy generation for master {master_id}: {source_path}")

    # Mark proxy as generating in the database
    _update_proxy_status(master_id, "generating", None)

    try:
        # Get source dimensions via ffprobe
        width, height = _get_dimensions(source_path, ffprobe_path)
        if not width or not height:
            raise ValueError("Could not determine source dimensions.")

        tw, th = _target_resolution(width, height)
        proxy_path = _proxy_path(source_path)

        logger.info(
            f"Master {master_id}: {width}x{height} -> {tw}x{th}, "
            f"output: {proxy_path}"
        )

        cmd = [
            ffmpeg_path,
            "-y",                          # overwrite if exists
            "-i", source_path,
            "-vf", f"scale={tw}:{th}",
            "-c:v", "libx264",
            "-profile:v", "main",
            "-level:v", "5.1",
            "-pix_fmt", "yuv420p",         # required for broad compatibility
            "-preset", "fast",
            "-b:v", PROXY_BITRATE,
            "-c:a", "aac",
            "-b:a", AUDIO_BITRATE,
            "-movflags", "+faststart",     # web-optimised MP4
            proxy_path,
        ]

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=600,  # 10 minute timeout
        )

        if result.returncode != 0:
            raise RuntimeError(f"ffmpeg error: {result.stderr[-500:]}")

        logger.info(f"Proxy generated for master {master_id}: {proxy_path}")
        _update_proxy_status(master_id, "ready", proxy_path)

    except Exception as e:
        logger.error(f"Proxy generation failed for master {master_id}: {e}")
        _update_proxy_status(master_id, "failed", None)


def generate_proxy_async(master_id: int, source_path: str) -> None:
    """
    Kick off proxy generation in a background thread.
    Returns immediately — does not block the web request.
    """
    thread = threading.Thread(
        target=generate_proxy,
        args=(master_id, source_path),
        daemon=True,
        name=f"proxy-{master_id}",
    )
    thread.start()
    logger.info(f"Proxy generation thread started for master {master_id}")


def _get_dimensions(filepath: str, ffprobe_path: str):
    """Return (width, height) of the first video stream, or (None, None)."""
    try:
        import json
        result = subprocess.run(
            [
                ffprobe_path,
                "-v", "quiet",
                "-print_format", "json",
                "-show_streams",
                "-select_streams", "v:0",
                filepath,
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        data = json.loads(result.stdout)
        streams = data.get("streams", [])
        if streams:
            return streams[0].get("width"), streams[0].get("height")
    except Exception as e:
        logger.error(f"ffprobe dimensions check failed: {e}")
    return None, None


def _update_proxy_status(master_id: int, status: str, proxy_path: Optional[str]) -> None:
    """Update the proxy_status and proxy_path columns on the master record."""
    conn = get_connection()
    conn.execute("""
        UPDATE masters SET proxy_status = ?, proxy_path = ? WHERE id = ?
    """, (status, proxy_path, master_id))
    conn.commit()
    conn.close()
