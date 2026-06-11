# -*- coding: utf-8 -*-
"""
STT factory for LiveKit turn-detector.

Default: local FunASR SenseVoiceSmall (CPU, no cloud keys).
Optional: LiveKit Cloud Inference STT.
"""
from __future__ import annotations

import logging
import os
from typing import Awaitable, Callable

from backend.funasr_sensevoice_stt_client import (
    CHUNK_BYTES,
    FunasrSenseVoiceStreamingSession,
    funasr_sensevoice_language,
    funasr_sensevoice_model,
)
from backend.livekit_inference_stt_client import (
    LivekitInferenceStreamingSession,
    livekit_inference_stt_language,
    livekit_inference_stt_model,
)

LOG = logging.getLogger(__name__)

_BACKENDS = frozenset({"funasr", "sensevoice", "local", "livekit_inference", "inference"})


def _load_cfg() -> dict:
    try:
        from pathlib import Path
        import json

        cfg_path = Path(__file__).resolve().parent.parent / "config.json"
        if cfg_path.exists():
            data = json.loads(cfg_path.read_text(encoding="utf-8"))
            block = (data.get("vad") or {}).get("livekit_turn") or {}
            if isinstance(block, dict):
                return block
    except Exception:
        pass
    return {}


def _stt_backend_override() -> str:
    env = (os.environ.get("LIVEKIT_TURN_STT", "") or os.environ.get("LIVEKIT_TURN_STT_BACKEND", "") or "").strip().lower()
    if env:
        if env in {"sensevoice", "local", "funasr"}:
            return "funasr"
        if env in {"inference", "livekit_inference", "livekit"}:
            return "livekit_inference"
        return env
    cfg = str(_load_cfg().get("stt_backend", "") or "").strip().lower()
    if cfg in {"sensevoice", "local", "funasr"}:
        return "funasr"
    if cfg in {"inference", "livekit_inference", "livekit"}:
        return "livekit_inference"
    if cfg in _BACKENDS:
        return cfg
    return ""


def livekit_turn_stt_backend() -> str:
    override = _stt_backend_override()
    if override:
        return override
    return "funasr"


def livekit_turn_stt_model_label() -> str:
    backend = livekit_turn_stt_backend()
    if backend == "funasr":
        return funasr_sensevoice_model()
    return livekit_inference_stt_model()


def livekit_turn_stt_language_label() -> str:
    backend = livekit_turn_stt_backend()
    if backend == "funasr":
        return funasr_sensevoice_language()
    return livekit_inference_stt_language()


def create_livekit_turn_stt_session(
    *,
    on_partial: Callable[[str, bool], Awaitable[None]] | None = None,
    on_notify: Callable[[str, str], Awaitable[None]] | None = None,
):
    backend = livekit_turn_stt_backend()
    if backend == "funasr":
        LOG.info(
            "LiveKit turn STT: FunASR SenseVoice (%s, %s, cpu)",
            funasr_sensevoice_model(),
            funasr_sensevoice_language(),
        )
        return FunasrSenseVoiceStreamingSession(
            model=funasr_sensevoice_model(),
            language=funasr_sensevoice_language(),
            on_partial=on_partial,
            on_notify=on_notify,
        )
    if backend == "livekit_inference":
        return LivekitInferenceStreamingSession(
            model=livekit_inference_stt_model(),
            language=livekit_inference_stt_language(),
            on_partial=on_partial,
            on_notify=on_notify,
        )
    raise RuntimeError(f"Unknown LIVEKIT_TURN_STT backend: {backend}")
