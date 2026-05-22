from __future__ import annotations

import json
import os
from pathlib import Path

from backend.duplug_vad_client import _load_duplug_vad_config


def _load_vad_json() -> dict:
    try:
        root = Path(__file__).resolve().parent.parent.parent
        cfg_path = root / "config.json"
        if cfg_path.exists():
            with cfg_path.open("r", encoding="utf-8") as f:
                data = json.load(f)
            vad = data.get("vad")
            if isinstance(vad, dict):
                return vad
    except Exception:
        pass
    return {}


def vad_provider() -> str:
    """
    Active VAD backend id.

    Priority: VAD_PROVIDER env → config.json vad.provider → smart_turn_v3.
    Legacy: if vad.provider unset and DUPLUG_VAD_ENABLED / duplug_enabled, use duplug.
    """
    env = (os.environ.get("VAD_PROVIDER", "") or "").strip().lower()
    if env:
        return env

    cfg = _load_vad_json()
    p = str(cfg.get("provider", "") or "").strip().lower()
    if p:
        return p

    duplug_on, _ = _load_duplug_vad_config()
    if duplug_on:
        return "duplug"
    return "smart_turn_v3"


def smart_turn_model_path() -> str | None:
    env = (os.environ.get("SMART_TURN_MODEL_PATH", "") or "").strip()
    if env:
        return env
    cfg = _load_vad_json().get("smart_turn") or {}
    if isinstance(cfg, dict):
        p = cfg.get("model_path")
        if p:
            return str(p)
    return None


def smart_turn_cpu_count() -> int:
    env = (os.environ.get("SMART_TURN_CPU_COUNT", "") or "").strip()
    if env.isdigit():
        return max(1, int(env))
    cfg = _load_vad_json().get("smart_turn") or {}
    if isinstance(cfg, dict):
        try:
            return max(1, int(cfg.get("cpu_count", 1)))
        except (TypeError, ValueError):
            pass
    return 1


def smart_turn_silero_stop_secs() -> float:
    env = (os.environ.get("SMART_TURN_SILERO_STOP_SECS", "") or "").strip()
    if env:
        try:
            return max(0.05, float(env))
        except ValueError:
            pass
    cfg = _load_vad_json().get("smart_turn") or {}
    if isinstance(cfg, dict) and cfg.get("silero_stop_secs") is not None:
        try:
            return max(0.05, float(cfg["silero_stop_secs"]))
        except (TypeError, ValueError):
            pass
    return 0.2


def smart_turn_silero_confidence() -> float:
    env = (os.environ.get("SMART_TURN_SILERO_CONFIDENCE", "") or "").strip()
    if env:
        try:
            return float(env)
        except ValueError:
            pass
    cfg = _load_vad_json().get("smart_turn") or {}
    if isinstance(cfg, dict) and cfg.get("silero_confidence") is not None:
        try:
            return float(cfg["silero_confidence"])
        except (TypeError, ValueError):
            pass
    return 0.7


def duplug_enabled_for_provider() -> bool:
    return vad_provider() == "duplug" and _load_duplug_vad_config()[0]
