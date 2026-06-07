#!/usr/bin/env python3
"""Smoke-test Hush (weya-ai/hush) assets and streaming API."""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend.hush.manager import create_hush_session, init_hush_manager, shutdown_hush_manager


def main() -> int:
    model = init_hush_manager()
    if model is None:
        print("FAIL: Hush manager did not initialize (check assets / OMNI_HUSH_ENABLED).")
        return 1

    session = create_hush_session()
    if session is None:
        print("FAIL: could not create Hush session.")
        shutdown_hush_manager()
        return 1

    frame_len = session.frame_length
    print(f"OK: frame_length={frame_len} samples @ 16 kHz")

    noisy = (np.random.randn(frame_len) * 0.05 * 32768).astype(np.int16)
    clean = session.process_pcm_s16le(noisy.tobytes())
    if len(clean) != frame_len * 2:
        print(f"FAIL: expected {frame_len * 2} bytes output, got {len(clean)}")
        session.close()
        shutdown_hush_manager()
        return 1

    print("OK: processed one int16 frame")
    session.close()
    shutdown_hush_manager()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
