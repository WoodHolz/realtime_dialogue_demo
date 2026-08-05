# -*- coding: utf-8 -*-
"""Prepare call recordings for IA-05 upload.

Size limits are enforced by the platform; we upload stereo.wav as-is.
"""
from __future__ import annotations

from pathlib import Path


def prepare_recording_for_upload(src: Path) -> Path:
    """Return the local stereo recording path for IA-05 upload."""
    if not src.is_file():
        raise FileNotFoundError(src)
    return src
