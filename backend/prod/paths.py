# -*- coding: utf-8 -*-
"""Shared prod data-root / env helpers (keeps TMP_PROD_* deploy vars working)."""
from __future__ import annotations

import os
from pathlib import Path


def env_first(*keys: str, default: str = "") -> str:
    """Return first non-empty env value among keys, else default."""
    for key in keys:
        raw = (os.environ.get(key) or "").strip()
        if raw:
            return raw
    return default


def prod_root() -> Path:
    """Canonical data root: PROD_ROOT → TMP_PROD_ROOT → existing data/tmp_prod → data/prod."""
    raw = env_first("PROD_ROOT", "TMP_PROD_ROOT")
    if raw:
        return Path(raw)
    legacy = Path("data/tmp_prod")
    if legacy.exists():
        return legacy
    return Path("data/prod")
