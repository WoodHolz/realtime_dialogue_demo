# -*- coding: utf-8 -*-
"""
STT factory for LiveKit turn-detector.

Default: local FunASR SenseVoiceSmall (CPU, no cloud keys).
Optional: Volc streaming ASR (Doubao) or LiveKit Cloud Inference STT.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Awaitable, Callable

from backend.asr_client import DEFAULT_ASR_RESOURCE_ID, VolcAsrStreamingSession
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

_BACKENDS = frozenset({"funasr", "sensevoice", "local", "volc", "livekit_inference", "inference"})


def _load_cfg() -> dict:
    try:
        cfg_path = Path(__file__).resolve().parent.parent / "config.json"
        if cfg_path.exists():
            data = json.loads(cfg_path.read_text(encoding="utf-8"))
            block = (data.get("vad") or {}).get("livekit_turn") or {}
            if isinstance(block, dict):
                return block
    except Exception:
        pass
    return {}


def normalize_livekit_stt_backend(raw: str | None) -> str | None:
    val = (raw or "").strip().lower()
    if not val:
        return None
    if val in {"sensevoice", "local", "funasr"}:
        return "funasr"
    if val in {"volc", "volcano", "doubao", "volc_streaming", "volc_asr", "豆包"}:
        return "volc"
    if val in {"inference", "livekit_inference", "livekit"}:
        return "livekit_inference"
    if val in _BACKENDS:
        return val
    return None


def _default_livekit_turn_stt_backend() -> str:
    env = (os.environ.get("LIVEKIT_TURN_STT", "") or os.environ.get("LIVEKIT_TURN_STT_BACKEND", "") or "").strip().lower()
    if env:
        norm = normalize_livekit_stt_backend(env)
        if norm:
            return norm
        return env
    cfg = str(_load_cfg().get("stt_backend", "") or "").strip().lower()
    norm = normalize_livekit_stt_backend(cfg)
    if norm:
        return norm
    return "funasr"


def resolve_livekit_turn_stt_backend(*, override: str | None = None) -> str:
    norm = normalize_livekit_stt_backend(override)
    if norm:
        return norm
    return _default_livekit_turn_stt_backend()


def livekit_turn_stt_backend() -> str:
    return _default_livekit_turn_stt_backend()


def livekit_turn_stt_display_label(backend: str | None = None) -> str:
    active = backend or resolve_livekit_turn_stt_backend()
    if active == "funasr":
        return "FunASR SenseVoice"
    if active == "volc":
        return "豆包流式 ASR"
    if active == "livekit_inference":
        return "LiveKit Inference STT"
    return f"LiveKit turn STT ({active})"


def livekit_turn_stt_model_label(backend: str | None = None) -> str:
    active = backend or resolve_livekit_turn_stt_backend()
    if active == "funasr":
        return funasr_sensevoice_model()
    if active == "volc":
        return os.environ.get("ASR_RESOURCE_ID", DEFAULT_ASR_RESOURCE_ID)
    return livekit_inference_stt_model()


def livekit_turn_stt_language_label(backend: str | None = None) -> str:
    active = backend or resolve_livekit_turn_stt_backend()
    if active == "funasr":
        return funasr_sensevoice_language()
    if active == "volc":
        return "zh-CN"
    return livekit_inference_stt_language()


def _volc_asr_credentials() -> tuple[str, str]:
    asr_app = (os.environ.get("ASR_APP_ID", "") or "").strip()
    asr_key = (os.environ.get("ASR_ACCESS_KEY", "") or "").strip()
    if asr_app and asr_key:
        return asr_app, asr_key
    try:
        cfg_path = Path(__file__).resolve().parent.parent / "config.json"
        if cfg_path.exists():
            data = json.loads(cfg_path.read_text(encoding="utf-8"))
            doubao = data.get("doubao", {}) or {}
            asr_app = asr_app or str(doubao.get("asr_app_id", "") or "").strip()
            asr_key = asr_key or str(doubao.get("asr_access_key", "") or "").strip()
            if asr_app and asr_key:
                return asr_app, asr_key
            app_id = str(doubao.get("app_id", "") or "").strip()
            access_key = str(doubao.get("access_key", "") or "").strip()
            if app_id and access_key:
                return app_id, access_key
    except Exception:
        pass
    app_id = (os.environ.get("DOUBAO_APP_ID", "") or "").strip()
    access_key = (os.environ.get("DOUBAO_ACCESS_KEY", "") or "").strip()
    return app_id, access_key


def create_livekit_turn_stt_session(
    *,
    stt_backend: str | None = None,
    on_partial: Callable[[str, bool], Awaitable[None]] | None = None,
    on_notify: Callable[[str, str], Awaitable[None]] | None = None,
    on_server_error: Callable[[int | None], Awaitable[None]] | None = None,
):
    backend = resolve_livekit_turn_stt_backend(override=stt_backend)
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
    if backend == "volc":
        app_id, access_key = _volc_asr_credentials()
        if not app_id or not access_key:
            raise RuntimeError(
                "豆包流式 ASR 凭证未配置：请设置 ASR_APP_ID + ASR_ACCESS_KEY "
                "或 config.json doubao.asr_app_id / asr_access_key"
            )
        rid = os.environ.get("ASR_RESOURCE_ID", DEFAULT_ASR_RESOURCE_ID)
        LOG.info("LiveKit turn STT: Volc streaming ASR (resource_id=%s, zh-CN)", rid)
        return VolcAsrStreamingSession(
            app_id,
            access_key,
            resource_id=rid,
            language="zh-CN",
            on_partial=on_partial,
            on_server_error=on_server_error,
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
