# -*- coding: utf-8 -*-
"""Independent LiveKit EoT / Turn Detector bench (eot-bench dataset).

Mirrors ``bench/`` layout: CLI + optional FastAPI routes, no pipeline session.
Uses official ``eot_harness`` adapters against ``livekit/eot-bench-data``.
"""

from pathlib import Path

EOT_BENCH_ROOT = Path(__file__).resolve().parent
REPO_ROOT = EOT_BENCH_ROOT.parent
WEB_DIR = EOT_BENCH_ROOT / "web"
DEFAULT_OUTPUT_DIR = EOT_BENCH_ROOT / "output"

DATASET_PATH = "livekit/eot-bench-data"
DEFAULT_SPLIT = "validation"
DEFAULT_LANGS = ("zh", "en")
