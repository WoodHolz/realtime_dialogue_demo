# -*- coding: utf-8 -*-
"""AEC feature flags and barge-in tuning when echo cancellation is active."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _load_audio_cfg() -> dict[str, Any]:
    cfg_path = _repo_root() / "config.json"
    try:
        data = json.loads(cfg_path.read_text(encoding="utf-8"))
        audio = data.get("audio")
        return audio if isinstance(audio, dict) else {}
    except Exception:
        return {}


def _env_flag(name: str, default: bool = False) -> bool:
    v = (os.environ.get(name, "") or "").strip().lower()
    if not v:
        return default
    return v in {"1", "true", "yes", "y", "on"}


def server_aec_enabled() -> bool:
    if _env_flag("SERVER_AEC_ENABLED"):
        return True
    audio = _load_audio_cfg()
    return bool(audio.get("server_aec_enabled", False))


def server_aec_delay_ms() -> int:
    env_v = (os.environ.get("SERVER_AEC_DELAY_MS", "") or "").strip()
    if env_v:
        try:
            return max(0, int(env_v))
        except ValueError:
            pass
    audio = _load_audio_cfg()
    try:
        return max(0, int(audio.get("server_aec_delay_ms", 80)))
    except (TypeError, ValueError):
        return 80


def client_aec_default_enabled() -> bool:
    audio = _load_audio_cfg()
    return bool(audio.get("client_aec_enabled", False))


def _base_barge_suppress_ms() -> int:
    env_v = (os.environ.get("T2S_TTS_BARGE_SUPPRESS_MS", "") or "").strip()
    if env_v:
        try:
            return max(0, int(env_v))
        except ValueError:
            pass
    try:
        data = json.loads((_repo_root() / "config.json").read_text(encoding="utf-8"))
        debug_cfg = data.get("debug") if isinstance(data.get("debug"), dict) else {}
        if "t2s_tts_barge_suppress_ms" in debug_cfg:
            return max(0, int(debug_cfg["t2s_tts_barge_suppress_ms"]))
    except Exception:
        pass
    return 400


def _barge_suppress_with_aec_ms() -> int:
    audio = _load_audio_cfg()
    try:
        return max(0, int(audio.get("t2s_tts_barge_suppress_ms_with_aec", 500)))
    except (TypeError, ValueError):
        return 500


def effective_barge_suppress_ms(*, client_aec: bool = False) -> int:
    """Shorter lead-in when hybrid AEC is active (server and/or client)."""
    if server_aec_enabled() or client_aec:
        return _barge_suppress_with_aec_ms()
    return _base_barge_suppress_ms()


def aec_status_dict(*, client_aec: bool = False) -> dict[str, Any]:
    return {
        "server_aec_enabled": server_aec_enabled(),
        "client_aec_enabled": bool(client_aec),
        "server_aec_delay_ms": server_aec_delay_ms(),
        "t2s_tts_barge_suppress_ms": effective_barge_suppress_ms(client_aec=client_aec),
    }
