#!/usr/bin/env python3
"""Thin wrapper — speed bench lives in `bench/` now.

Prefer:
  python -m bench.cli speed ...
  open http://127.0.0.1:7998/bench
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bench.cli import main  # noqa: E402

if __name__ == "__main__":
    # Preserve old flag style by defaulting to the speed subcommand.
    argv = sys.argv[1:]
    if not argv or argv[0] not in {"speed", "recovery", "-h", "--help"}:
        argv = ["speed", *argv]
    main(argv)
