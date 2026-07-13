"""
qc_checks.py — Automated QC scanning for ingested masters.

Runs blanking and duplicate-frame detection using the SlateDetector from
TechOps Tools (ported to qcgate/detection/slate_detector.py).

Called from ingest.py and conflicts.py after a new master or iteration
record is created.  Runs in a background thread so ingest is not blocked.

Results are stored as JSON in iterations.qc_flags.
If issues are found, master and iteration status are set to 'Flagged'.
If clean, status stays 'Awaiting QC'.
"""

import json
import logging
import os
import threading
from datetime import datetime
from typing import Optional, List, Dict

from qcgate.database import get_connection

# Maximum number of QC scans that run concurrently.
# Read from config at startup; change takes effect after watcher restart.
def _qc_scan_concurrency() -> int:
    try:
        from qcgate import config as _config
        return max(1, int(_config.get("qc_scan_concurrency") or 2))
    except (ValueError, TypeError):
        return 2

_qc_semaphore = threading.Semaphore(_qc_scan_concurrency())

logger = logging.getLogger(__name__)


def _run_qc_checks(master_id: int, iteration_number: int, filepath: str) -> None:
    """Worker: run SlateDetector and store results.  Called in a daemon thread."""
    try:
        from qcgate.detection.slate_detector import SlateDetector, HAS_CV2
    except Exception as e:
        logger.error(f"QC checks: could not import SlateDetector: {e}")
        _set_scan_status(master_id, iteration_number, "failed")
        return

    if not HAS_CV2:
        logger.warning("QC checks: OpenCV not installed — skipping scan for master %d", master_id)
        _set_scan_status(master_id, iteration_number, "failed")
        return

    logger.info("QC checks: queued scan for master %d iteration %d", master_id, iteration_number)
    _qc_semaphore.acquire()
    logger.info("QC checks: starting scan for master %d iteration %d: %s",
                master_id, iteration_number, filepath)

    try:
        detector = SlateDetector(filepath)
        results = detector.analyze()
    except Exception as e:
        logger.error("QC checks: SlateDetector crashed for master %d: %s", master_id, e)
        _set_scan_status(master_id, iteration_number, "failed")
        _qc_semaphore.release()
        return

    if "error" in results:
        logger.error("QC checks: scan error for master %d: %s", master_id, results["error"])
        _set_scan_status(master_id, iteration_number, "failed")
        _qc_semaphore.release()
        return

    duplicate_frames = results.get("duplicate_frames", [])
    blanking_data = results.get("blanking", {})
    blanking_segments = blanking_data.get("segments", [])

    has_duplicates = bool(duplicate_frames)
    has_blanking = blanking_data.get("has_blanking", False)
    has_issues = has_duplicates or has_blanking

    # Save frame images if a frames directory is configured
    from qcgate import config as qcgate_config
    frames_dir = qcgate_config.get("qc_frames_path") or ""
    scan_stamp = datetime.now().strftime("%Y%m%d%H%M")

    def _save_blanking_frame(frame_number, timecode, confidence, blanking):
        # type: (int, str, float, dict) -> Optional[str]
        """
        Extract a frame, apply dark-pixel false-colour (green highlight), save as JPEG.
        Pixels below brightness threshold 20 are replaced with green — same threshold
        the detector uses — so blanking regions light up clearly.
        """
        if not frames_dir:
            return None
        try:
            import cv2
            import numpy as np
            os.makedirs(frames_dir, exist_ok=True)
            cap = cv2.VideoCapture(filepath)
            cap.set(cv2.CAP_PROP_POS_FRAMES, frame_number)
            ok, frame = cap.read()
            cap.release()
            if not ok or frame is None:
                return None

            # False-colour: replace dark pixels with green.
            # TechOps Tools uses threshold 20 visually; our grayscale conversion
            # produces slightly higher values for the same pixels, so 13 is calibrated
            # to produce equivalent coverage.
            DARK_THRESHOLD = 13
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            dark_mask = gray <= DARK_THRESHOLD
            annotated = frame.copy()
            annotated[dark_mask] = (0, 255, 0)  # BGR green

            # Info bar at bottom — black band with white text
            h, w = annotated.shape[:2]
            bar_h = max(28, h // 30)
            bar = np.zeros((bar_h, w, 3), dtype=np.uint8)
            sides = []
            if blanking.get("left"):
                sides.append("L:%dpx" % blanking["left"])
            if blanking.get("right"):
                sides.append("R:%dpx" % blanking["right"])
            if blanking.get("top"):
                sides.append("T:%dpx" % blanking["top"])
            if blanking.get("bottom"):
                sides.append("B:%dpx" % blanking["bottom"])
            label = "%s  |  %s  |  conf: %.0f%%" % (timecode, "  ".join(sides), confidence)
            font_scale = bar_h / 40.0
            cv2.putText(bar, label, (8, bar_h - 7),
                        cv2.FONT_HERSHEY_SIMPLEX, font_scale, (220, 220, 220), 1, cv2.LINE_AA)
            annotated = np.vstack([annotated, bar])

            tc_safe = timecode.replace(":", "").replace(";", "")
            filename = "%d_%s_%s.jpg" % (master_id, scan_stamp, tc_safe)
            dest = os.path.join(frames_dir, filename)
            cv2.imwrite(dest, annotated, [cv2.IMWRITE_JPEG_QUALITY, 90])
            return filename
        except Exception as e:
            logger.warning("QC checks: could not save frame image: %s", e)
            return None

    # Slim the segments — strip confidence_details, save one annotated JPEG per segment
    slim_segments = []
    for seg in blanking_segments:
        tc = seg.get("start_tc", "")
        frame_num = seg.get("start_frame", 0)
        confidence = seg.get("confidence", 0)
        blanking_px = {k: seg.get("blanking", {}).get(k, 0) for k in ("left", "right", "top", "bottom")}
        frame_filename = _save_blanking_frame(frame_num, tc, confidence, blanking_px)
        slim_segments.append({
            "start_tc": tc,
            "end_tc": seg.get("end_tc"),
            "duration_frames": seg.get("duration_frames"),
            "confidence": round(confidence, 1),
            "severity": seg.get("severity"),
            "blanking": blanking_px,
            "frame_filename": frame_filename,
        })

    slim_duplicates = [
        {"frame": d["frame"], "timecode": d["timecode"], "mse": round(d["mse"], 2)}
        for d in duplicate_frames
    ]

    qc_flags = {
        "duplicate_frames": slim_duplicates,  # timecodes only, no images
        "blanking": {
            "has_blanking": has_blanking,
            "segments": slim_segments,
            "max": blanking_data.get("max", {"left": 0, "right": 0, "top": 0, "bottom": 0}),
        },
    }

    new_status = "Flagged" if has_issues else "Awaiting QC"

    conn = get_connection()
    conn.execute("""
        UPDATE iterations
        SET qc_flags = ?, qc_scan_status = 'complete', status = ?
        WHERE master_id = ? AND iteration_number = ?
    """, (json.dumps(qc_flags), new_status, master_id, iteration_number))

    if has_issues:
        conn.execute("UPDATE masters SET status = 'Flagged' WHERE id = ?", (master_id,))
    else:
        conn.execute("UPDATE masters SET status = 'Awaiting QC' WHERE id = ?", (master_id,))

    conn.commit()
    conn.close()

    _qc_semaphore.release()
    logger.info(
        "QC checks: master %d iter %d — %s (%d dup frames, %d blanking segments)",
        master_id, iteration_number, new_status,
        len(duplicate_frames), len(slim_segments),
    )


def _set_scan_status(master_id: int, iteration_number: int, status: str) -> None:
    try:
        conn = get_connection()
        conn.execute(
            "UPDATE iterations SET qc_scan_status = ?, status = 'Awaiting QC' WHERE master_id = ? AND iteration_number = ?",
            (status, master_id, iteration_number),
        )
        conn.execute("UPDATE masters SET status = 'Awaiting QC' WHERE id = ?", (master_id,))
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error("QC checks: could not update scan status: %s", e)


def run_qc_checks_async(master_id: int, iteration_number: int, filepath: str) -> None:
    """
    Mark the iteration as 'pending' scan and kick off the check in a daemon thread.
    Returns immediately — caller is not blocked.
    """
    try:
        conn = get_connection()
        conn.execute(
            "UPDATE iterations SET qc_scan_status = 'pending' WHERE master_id = ? AND iteration_number = ?",
            (master_id, iteration_number),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error("QC checks: could not set pending status: %s", e)
        return

    t = threading.Thread(
        target=_run_qc_checks,
        args=(master_id, iteration_number, filepath),
        daemon=True,
        name="qc-checks-%d-%d" % (master_id, iteration_number),
    )
    t.start()
