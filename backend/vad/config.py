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


LOCAL_SEMANTIC_VAD_PROVIDERS = frozenset({"smart_turn_v3", "livekit_turn_detector"})


def normalize_vad_provider(value: str | None) -> str | None:
    """Map UI / legacy aliases to canonical provider ids."""
    p = (value or "").strip().lower()
    if not p:
        return None
    if p in {"smart_turn", "smart-turn", "smart-turn-v3", "smart_turn_v3"}:
        return "smart_turn_v3"
    if p in {"livekit", "livekit_turn", "livekit-turn-detector", "livekit_turn_detector"}:
        return "livekit_turn_detector"
    if p in {"duplug"}:
        return "duplug"
    if p in LOCAL_SEMANTIC_VAD_PROVIDERS:
        return p
    return None


def resolve_vad_provider(
    *,
    cfg: dict | None = None,
    override: str | None = None,
) -> str:
    """
    Resolve VAD provider for a session.

    Priority: explicit override → cfg vad_provider / local_vad_provider →
    cfg omni_vad_mode (if semantic) → global vad_provider().
    """
    for raw in (override,):
        normalized = normalize_vad_provider(raw)
        if normalized:
            return normalized

    data = cfg or {}
    for key in ("vad_provider", "local_vad_provider"):
        normalized = normalize_vad_provider(str(data.get(key, "") or ""))
        if normalized:
            return normalized

    mode = (data.get("omni_vad_mode") or "").strip().lower()
    if mode in {"dashscope", "server", "server_vad", "semantic_vad", "alibaba", "ali"}:
        pass
    else:
        normalized = normalize_vad_provider(mode)
        if normalized:
            return normalized
        if mode in {"local", "local_vad"}:
            # Legacy clients sent omni_vad_mode=local without vad_provider; prefer env/config default.
            return vad_provider()

    return vad_provider()


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


def is_local_semantic_vad_provider(provider: str | None = None) -> bool:
    p = (provider or vad_provider()).strip().lower()
    return p in LOCAL_SEMANTIC_VAD_PROVIDERS or p == "duplug"


def livekit_turn_language() -> str:
    env = (os.environ.get("LIVEKIT_TURN_LANGUAGE", "") or "").strip()
    if env:
        return env
    cfg = _load_vad_json().get("livekit_turn") or {}
    if isinstance(cfg, dict) and cfg.get("language"):
        return str(cfg["language"])
    return "zh"


def livekit_turn_threshold_override() -> float | None:
    env = (os.environ.get("LIVEKIT_TURN_THRESHOLD", "") or "").strip()
    if env:
        try:
            return float(env)
        except ValueError:
            pass
    cfg = _load_vad_json().get("livekit_turn") or {}
    if isinstance(cfg, dict) and cfg.get("threshold") is not None:
        try:
            return float(cfg["threshold"])
        except (TypeError, ValueError):
            pass
    return None


def livekit_turn_embedded_asr_enabled() -> bool:
    env = (os.environ.get("LIVEKIT_TURN_EMBEDDED_ASR", "") or "").strip().lower()
    if env in {"0", "false", "no"}:
        return False
    if env in {"1", "true", "yes"}:
        return True
    cfg = _load_vad_json().get("livekit_turn") or {}
    if isinstance(cfg, dict) and cfg.get("embedded_asr") is not None:
        return bool(cfg["embedded_asr"])
    return True
