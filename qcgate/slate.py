"""
slate.py — Extract metadata from the slate/clock frame burned into a master video.

Pipeline:
  1. ffmpeg extracts a single frame at 1 second as a temp PNG
  2. pytesseract runs OCR on the frame
  3. Parser scans lines for known keywords and extracts values
  4. Aspect ratio falls back to derivation from resolution if not on the slate

Returns a dict: {title, version, clock, aspect}
Any field not found will be None.
"""

import os
import re
import logging
import tempfile
import subprocess
from typing import Optional, Dict, List

logger = logging.getLogger(__name__)


def extract_slate_metadata(
    filepath: str,
    ffmpeg_path: str,
    tesseract_path: str,
    resolution: Optional[str] = None,
) -> Dict[str, Optional[str]]:
    """
    Extract slate metadata from a video file.

    Args:
        filepath:       Full path to the video file.
        ffmpeg_path:    Full path to the ffmpeg binary.
        tesseract_path: Full path to the tesseract binary.
        resolution:     Resolution string from ffprobe e.g. "1920x1080".
                        Used as fallback for aspect ratio if not found on slate.

    Returns:
        Dict with keys: title, version, clock, aspect.
        Any field not detected will be None.
    """
    empty: Dict[str, Optional[str]] = {
        "title": None,
        "version": None,
        "clock": None,
        "aspect": None,
        "duration": None,
    }

    if not ffmpeg_path or not tesseract_path:
        logger.warning("slate: ffmpeg_path or tesseract_path not configured — skipping OCR")
        return empty

    if not os.path.exists(filepath):
        logger.warning(f"slate: file not found — {filepath}")
        return empty

    with tempfile.TemporaryDirectory() as tmpdir:
        frame_path = os.path.join(tmpdir, "slate_frame.png")

        # Extract frame at 1 second
        try:
            result = subprocess.run(
                [
                    ffmpeg_path,
                    "-y",
                    "-ss", "1",
                    "-i", filepath,
                    "-frames:v", "1",
                    "-q:v", "2",
                    frame_path,
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )
        except FileNotFoundError:
            logger.error(f"slate: ffmpeg not found at {ffmpeg_path}")
            return empty
        except subprocess.TimeoutExpired:
            logger.error(f"slate: ffmpeg timed out extracting frame from {filepath}")
            return empty
        except Exception as e:
            logger.error(f"slate: unexpected error running ffmpeg on {filepath}: {e}")
            return empty

        if result.returncode != 0 or not os.path.exists(frame_path):
            logger.warning(f"slate: ffmpeg failed to extract frame from {filepath}: {result.stderr[-300:]}")
            return empty

        # Run OCR
        try:
            import pytesseract
            from PIL import Image

            pytesseract.pytesseract.tesseract_cmd = tesseract_path
            img = Image.open(frame_path)
            ocr_text = pytesseract.image_to_string(img)
        except ImportError as e:
            logger.error(f"slate: missing dependency — {e}. Install pytesseract and Pillow.")
            return empty
        except Exception as e:
            logger.error(f"slate: OCR failed for {filepath}: {e}")
            return empty

    logger.debug(f"slate: raw OCR text for {os.path.basename(filepath)}:\n{ocr_text!r}")

    if not ocr_text or not ocr_text.strip():
        logger.info(f"slate: OCR returned no text for {filepath}")
        result_dict = dict(empty)
        if resolution:
            result_dict["aspect"] = _derive_aspect(resolution)
        return result_dict

    parsed = _parse_slate_text(ocr_text)

    if parsed["aspect"]:
        parsed["aspect"] = _normalise_aspect(parsed["aspect"])

    # Fall back to resolution-derived aspect if slate didn't have one
    if not parsed["aspect"] and resolution:
        parsed["aspect"] = _derive_aspect(resolution)

    logger.info(
        f"slate: extracted from {os.path.basename(filepath)} — "
        f"title={parsed['title']!r} version={parsed['version']!r} "
        f"clock={parsed['clock']!r} aspect={parsed['aspect']!r} duration={parsed['duration']!r}"
    )

    return parsed


_CLOCK_TYPE_SUFFIXES = {
    "ONLINE", "DOOH", "OLV", "SOCIAL", "OOH", "CTV", "VOD",
    "DISPLAY", "CINEMA", "AUDIO",
}


def extract_clock_from_filename(filename: str) -> Optional[str]:
    """
    Determine a clock number from filename structure alone, for masters that
    carry a clock number but have no burned-in slate to OCR.

    Looks for a structural triple PREFIX_NAME_030 — a 3-4 letter prefix, a
    longer identifier segment, then either a 3-digit duration or a known
    deliverable-type suffix (ONLINE, DOOH, SOCIAL, etc). Hyphens are
    normalised to underscores first (MUM-KNCM965-010 -> MUM_KNCM965_010).
    """
    stem = os.path.splitext(filename)[0]
    parts = stem.replace("-", "_").split("_")

    for i in range(len(parts) - 2):
        prefix, ident, tail = parts[i], parts[i + 1], parts[i + 2]
        if not re.fullmatch(r"[A-Za-z]{3,4}", prefix):
            continue
        if len(ident) <= len(prefix):
            continue
        if re.fullmatch(r"\d{3}", tail) or tail.upper() in _CLOCK_TYPE_SUFFIXES:
            return "_".join((prefix, ident, tail))

    return None


def _parse_slate_text(text: str) -> Dict[str, Optional[str]]:
    """
    Parse OCR text from a slate frame and extract known fields.

    Tries two strategies in order:

    1. Inline — label and value on the same line:
           TITLE   6" Hero Cutdown
       Common when Tesseract reads a slate row by row.

    2. Columnar — Tesseract reads a two-column slate as two separate blocks:
           PRODUCT          <- labels block (one per line)
           TITLE
           VERSION
           ...
           Altos Plata      <- values block (positionally matched to labels)
           6" Hero Cutdown
           UK TITLED - Cut A
           ...
    """
    # Label definitions — ordered longest-first to avoid partial keyword matches.
    # Field is None for labels we recognise but don't capture in our schema.
    LABELS = [
        ("aspect ratio", "aspect"),
        ("clock no",     "clock"),
        ("version",      "version"),
        ("duration",     "duration"),
        ("product",      None),
        ("client",       None),
        ("agency",       None),
        ("title",        "title"),
        ("ver",          "version"),
        ("clock",        "clock"),
        ("aspect",       "aspect"),
        ("ar",           "aspect"),
        ("frame rate",   None),
        ("date",         None),
        ("info",         None),
        ("audio",        None),
        ("advertiser",   None),
        ("brand",        None),
        ("format",       None),
    ]

    lines = [line.strip() for line in text.splitlines()]

    result = _try_inline(lines, LABELS)
    if any(v is not None for v in result.values()):
        return result

    return _try_columnar(lines, LABELS)


def _try_inline(lines: List[str], LABELS: list) -> Dict[str, Optional[str]]:
    """
    Inline strategy: each line contains "KEYWORD   value".
    Returns whatever fields could be found; caller checks if any were non-None.
    """
    result: Dict[str, Optional[str]] = {
        "title": None, "version": None, "clock": None, "aspect": None, "duration": None,
    }
    all_keywords = {kw for kw, _ in LABELS}

    for i, line in enumerate(lines):
        if not line:
            continue
        lower = line.lower()

        for keyword, field in LABELS:
            if field is None or result[field] is not None:
                continue
            if keyword not in lower:
                continue

            idx = lower.find(keyword)
            end_idx = idx + len(keyword)

            # Reject substring match — "title" must not match inside "titled"
            if end_idx < len(lower) and lower[end_idx].isalpha():
                continue

            after = line[end_idx:].strip().lstrip(":").strip()

            if after:
                value = after
                # If the next non-empty line doesn't start with an all-caps
                # label word, treat it as a wrapped continuation of this value
                # (e.g. a title that Tesseract split across two lines).
                for j in range(i + 1, min(i + 3, len(lines))):
                    if not lines[j]:
                        continue
                    first_word = lines[j].split()[0].rstrip(".,:-") if lines[j].split() else ""
                    if first_word and not first_word.isupper():
                        value = value + " " + lines[j]
                    break
                result[field] = value
            else:
                # Keyword alone on its line — look at next non-empty line.
                # Reject it if it looks like a label (known keyword OR starts
                # with an all-caps word, which catches multi-word labels like
                # FRAME RATE that may not be in the keyword list).
                for j in range(i + 1, min(i + 3, len(lines))):
                    if not lines[j]:
                        continue
                    if any(kw in lines[j].lower() for kw in all_keywords):
                        break
                    first_word = lines[j].split()[0].rstrip(".,:-") if lines[j].split() else ""
                    if first_word and first_word.isupper() and len(first_word) > 1:
                        break
                    result[field] = lines[j]
                    break
            break

    return result


def _try_columnar(lines: List[str], LABELS: list) -> Dict[str, Optional[str]]:
    """
    Columnar strategy: find a consecutive block of label-only lines, then
    positionally match the non-empty value lines that follow.
    """
    result: Dict[str, Optional[str]] = {
        "title": None, "version": None, "clock": None, "aspect": None, "duration": None,
    }

    def match_label(line: str) -> Optional[str]:
        """
        If this line is entirely a known label (nothing substantial after it),
        return the field name (or None for recognised-but-not-captured labels).
        Returns the sentinel "NO_MATCH" if the line is not a label.
        """
        lower = line.lower()
        for keyword, field in LABELS:
            if not lower.startswith(keyword):
                continue
            remainder = lower[len(keyword):].strip().lstrip(":")
            # Accept only if nothing substantive follows (not an inline line)
            if not remainder:
                return field  # may be None for ignored labels
        return "NO_MATCH"

    # Find the contiguous label block
    label_sequence: List[Optional[str]] = []
    label_block_end = 0
    started = False

    for i, line in enumerate(lines):
        if not line:
            if started:
                # Blank line within the label block — peek ahead to see if more
                # labels follow (Tesseract sometimes inserts a blank between label
                # groups, e.g. PRODUCT..DATE [blank] INFO [blank] values...).
                more_labels = False
                for j in range(i + 1, len(lines)):
                    if not lines[j]:
                        continue
                    if match_label(lines[j]) != "NO_MATCH":
                        more_labels = True
                    break
                if not more_labels:
                    label_block_end = i
                    break
            continue
        field = match_label(line)
        if field != "NO_MATCH":
            label_sequence.append(field)
            started = True
        elif started:
            label_block_end = i
            break

    if not label_sequence:
        return result

    # Every non-empty line after the label block is a value, matched positionally
    values = [line for line in lines[label_block_end:] if line]

    for i, field in enumerate(label_sequence):
        if i >= len(values):
            break
        if field and result[field] is None:
            result[field] = values[i]

    return result


def _normalise_aspect(value: str) -> str:
    """
    Tesseract frequently drops the colon from aspect ratios (e.g. '45' for '4:5',
    '169' for '16:9').  Re-insert it for known ratios and common OCR variants.
    """
    KNOWN = {
        "11": "1:1",
        "43": "4:3",
        "45": "4:5",
        "54": "5:4",
        "169": "16:9",
        "916": "9:16",
        "166": "1.66",
        "185": "1.85",
        "239": "2.39",
        "235": "2.35",
    }
    stripped = value.replace(":", "").replace(".", "").replace(" ", "")
    if stripped in KNOWN:
        return KNOWN[stripped]
    # If it already contains a colon or decimal it's fine as-is
    return value


def _derive_aspect(resolution: str) -> Optional[str]:
    """
    Derive an aspect ratio string from a resolution string e.g. "1920x1080" -> "16:9".
    Returns None if the resolution cannot be parsed.
    """
    try:
        parts = resolution.lower().split("x")
        if len(parts) != 2:
            return None
        width = int(parts[0])
        height = int(parts[1])
        if height == 0:
            return None

        from math import gcd
        g = gcd(width, height)
        return f"{width // g}:{height // g}"
    except (ValueError, AttributeError):
        return None
