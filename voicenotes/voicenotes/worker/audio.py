"""Délka nahrávky přes ffprobe. Když ffprobe není, klíč ve frontmatteru chybí."""

from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path

log = logging.getLogger("voicenotes.worker.audio")


def probe_duration_s(path: Path, *, ffprobe_path: str = "ffprobe") -> int | None:
    command = [
        ffprobe_path,
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "json",
        str(path),
    ]
    try:
        result = subprocess.run(
            command, capture_output=True, text=True, timeout=30, check=False
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.debug("ffprobe nedostupný (%s), duration_s vynechán", exc)
        return None
    if result.returncode != 0:
        log.debug("ffprobe selhal na %s: %s", path.name, result.stderr.strip())
        return None
    try:
        duration = float(json.loads(result.stdout)["format"]["duration"])
    except (ValueError, KeyError, TypeError, json.JSONDecodeError):
        return None
    return round(duration)
