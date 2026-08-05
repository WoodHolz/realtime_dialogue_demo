# -*- coding: utf-8 -*-
"""IA-05 recording upload helpers."""
from __future__ import annotations

import wave
from pathlib import Path

import pytest

from backend.ia.recording_upload import prepare_recording_for_upload


def _write_stereo_16k(path: Path, *, seconds: float = 0.05) -> None:
    rate = 16000
    n = int(rate * seconds)
    frames = b"\x00\x00\x00\x00" * n
    with wave.open(str(path), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(frames)


def test_prepare_passthrough(tmp_path: Path):
    src = tmp_path / "stereo.wav"
    _write_stereo_16k(src)
    assert prepare_recording_for_upload(src) == src


def test_prepare_missing_raises(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        prepare_recording_for_upload(tmp_path / "missing.wav")
