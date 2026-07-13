"""
ffprobe.py — Extract technical metadata from media files using ffprobe.

Called at ingest time when a new file is detected in the watch folder.
Returns a dict of metadata fields to be stored in the iterations table.
"""

import subprocess
import json
import logging
from typing import Optional, Dict

logger = logging.getLogger(__name__)


def extract_metadata(filepath: str, ffprobe_path: str) -> Dict[str, Optional[str]]:
    """
    Run ffprobe on a file and return a dict of technical metadata.

    Returns a dict with keys: codec, resolution, framerate, duration, audio_channels.
    Any field that cannot be determined will be None.
    """
    empty = {
        "codec": None,
        "resolution": None,
        "framerate": None,
        "duration": None,
        "audio_channels": None,
        "scan_type": None,
    }

    try:
        result = subprocess.run(
            [
                ffprobe_path,
                "-v", "quiet",
                "-print_format", "json",
                "-show_streams",
                "-show_format",
                filepath,
            ],
            capture_output=True,
            text=True,
            timeout=45,
        )

        if result.returncode != 0:
            logger.error(f"ffprobe failed for {filepath}: {result.stderr}")
            return empty

        data = json.loads(result.stdout)
        streams = data.get("streams", [])
        fmt = data.get("format", {})

        metadata = dict(empty)

        # --- Video stream ---
        video = next((s for s in streams if s.get("codec_type") == "video"), None)
        if video:
            # Resolve ProRes variant from codec_tag_string
            codec_name = video.get("codec_name", "")
            codec_tag = video.get("codec_tag_string", "")
            if codec_name == "prores":
                prores_map = {
                    "apco": "ProRes 422 Proxy",
                    "apcs": "ProRes 422 LT",
                    "apcn": "ProRes 422",
                    "apch": "ProRes 422HQ",
                    "ap4h": "ProRes 4444",
                    "ap4x": "ProRes 4444 XQ",
                }
                metadata["codec"] = prores_map.get(codec_tag.lower(), f"ProRes ({codec_tag})")
            else:
                metadata["codec"] = codec_name

            width = video.get("width")
            height = video.get("height")
            if width and height:
                metadata["resolution"] = f"{width}x{height}"

            # Scan type — interlaced or progressive
            field_order = video.get("field_order", "progressive")
            if field_order in ("tt", "bb", "tb", "bt"):
                metadata["scan_type"] = "Interlaced"
            else:
                metadata["scan_type"] = "Progressive"

            # Framerate is stored as a fraction e.g. "25/1" or "30000/1001"
            r_frame_rate = video.get("r_frame_rate")
            if r_frame_rate:
                try:
                    num, den = r_frame_rate.split("/")
                    fps = round(int(num) / int(den), 3)
                    fps_str = f"{int(fps)}fps" if fps == int(fps) else f"{fps}fps"
                    metadata["framerate"] = fps_str
                except (ValueError, ZeroDivisionError):
                    metadata["framerate"] = r_frame_rate

            # Duration from format block (more reliable than stream duration)
            duration_secs = fmt.get("duration")
            if duration_secs:
                try:
                    secs = float(duration_secs)
                    hours = int(secs // 3600)
                    mins = int((secs % 3600) // 60)
                    secs_rem = int(secs % 60)
                    metadata["duration"] = f"{hours:02d}:{mins:02d}:{secs_rem:02d}"
                except ValueError:
                    metadata["duration"] = duration_secs

        # --- Audio stream(s) ---
        audio_streams = [s for s in streams if s.get("codec_type") == "audio"]
        if audio_streams:
            total_channels = sum(s.get("channels", 0) for s in audio_streams)
            metadata["audio_channels"] = str(total_channels)

        return metadata

    except FileNotFoundError:
        logger.error(f"ffprobe not found at path: {ffprobe_path}")
        return empty
    except subprocess.TimeoutExpired:
        logger.error(f"ffprobe timed out for: {filepath}")
        return empty
    except json.JSONDecodeError as e:
        logger.error(f"ffprobe returned invalid JSON for {filepath}: {e}")
        return empty
    except Exception as e:
        logger.error(f"Unexpected error running ffprobe on {filepath}: {e}")
        return empty


def measure_loudness(filepath: str, ffmpeg_path: str) -> Optional[str]:
    """
    Measure integrated loudness (LUFS) using ffmpeg's ebur128 filter.
    Returns a string like '-23.5 LUFS' or None if measurement fails.
    """
    if not ffmpeg_path:
        return None
    try:
        result = subprocess.run(
            [
                ffmpeg_path,
                "-i", filepath,
                "-af", "ebur128=peak=true",
                "-f", "null", "-",
            ],
            capture_output=True,
            text=True,
            timeout=180,
        )
        # ebur128 summary is written to stderr
        for line in reversed(result.stderr.splitlines()):
            line = line.strip()
            if line.startswith("I:"):
                value = line.split("I:")[1].strip().split()[0]
                return f"{value} LUFS"
        return None
    except subprocess.TimeoutExpired:
        logger.error(f"ffmpeg loudness measurement timed out for: {filepath}")
        return None
    except Exception as e:
        logger.error(f"Loudness measurement failed for {filepath}: {e}")
        return None
