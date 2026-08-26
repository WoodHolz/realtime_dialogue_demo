# -*- coding: utf-8 -*-
"""Pipeline adapter settings (env > config.json). No vendor client types here."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from backend.pipeline.adapters.llm_stub import StubLlm
from backend.pipeline.adapters.tts_stub import StubTts
from backend.pipeline.protocols import StreamingLlm, StreamingTts

DEFAULT_LLM_MODEL = "qwen3.5-flash"
DEFAULT_TTS_MODEL = "qwen-audio-3.0-tts-plus"
DEFAULT_TTS_VOICE = "longanlingxi"
PUBLIC_LLM_BASE = "https://dashscope.aliyuncs.com/compatible-mode/v1"
PUBLIC_TTS_WS = "wss://dashscope.aliyuncs.com/api-ws/v1/inference"
_LEGACY_RESPONSES_PREFIX = "/api/v2/apps/protocols/compatible-mode/v1"


def _root() -> Path:
    return Path(__file__).resolve().parent.parent.parent


def _load_json() -> dict[str, Any]:
    path = _root() / "config.json"
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _truthy(raw: str | None, default: bool) -> bool:
    if raw is None or not str(raw).strip():
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _clamp_speech_rate(raw: object, default: float = 1.2) -> float:
    """DashScope speech_rate is [0.5, 2.0]."""
    try:
        value = float(raw)
    except (TypeError, ValueError):
        value = float(default)
    return max(0.5, min(2.0, value))


def normalize_llm_base_url(url: str) -> str:
    """Responses API lives at {host}/compatible-mode/v1, not the deprecated v2 apps path."""
    cleaned = (url or "").strip().rstrip("/")
    if _LEGACY_RESPONSES_PREFIX in cleaned:
        cleaned = cleaned.replace(_LEGACY_RESPONSES_PREFIX, "/compatible-mode/v1")
        cleaned = cleaned.replace("/compatible-mode/v1/compatible-mode/v1", "/compatible-mode/v1")
    return cleaned


def workspace_llm_base(workspace_id: str, *, region: str = "cn-beijing") -> str:
    """Dedicated MaaS host + new Responses path. Not used until we migrate off dashscope.aliyuncs.com."""
    ws = (workspace_id or "").strip()
    return f"https://{ws}.{region}.maas.aliyuncs.com/compatible-mode/v1"


def workspace_tts_ws(workspace_id: str, *, region: str = "cn-beijing") -> str:
    ws = (workspace_id or "").strip()
    return f"wss://{ws}.{region}.maas.aliyuncs.com/api-ws/v1/inference"


def llm_base_url(*, workspace_id: str = "", base_url: str = "") -> str:
    # workspace_id is loaded but ignored: stay on public dashscope.aliyuncs.com for now.
    _ = workspace_id
    explicit = normalize_llm_base_url(base_url)
    if explicit:
        return explicit
    return PUBLIC_LLM_BASE


def tts_ws_url(*, workspace_id: str = "", ws_url: str = "") -> str:
    _ = workspace_id
    explicit = (ws_url or "").strip()
    if explicit:
        return explicit
    return PUBLIC_TTS_WS


@dataclass(frozen=True)
class PipelineSettings:
    llm_adapter: str = "dashscope_responses"
    tts_adapter: str = "dashscope_tts"
    api_key: str = ""
    workspace_id: str = ""
    llm_base_url: str = ""
    llm_model: str = DEFAULT_LLM_MODEL
    enable_thinking: bool = False
    tts_ws_url: str = ""
    tts_model: str = DEFAULT_TTS_MODEL
    tts_voice: str = DEFAULT_TTS_VOICE
    tts_sample_rate: int = 24000
    tts_speech_rate: float = 1.2

    @property
    def resolved_llm_base(self) -> str:
        return llm_base_url(workspace_id=self.workspace_id, base_url=self.llm_base_url)

    @property
    def resolved_tts_ws(self) -> str:
        return tts_ws_url(workspace_id=self.workspace_id, ws_url=self.tts_ws_url)

    def describe(self) -> dict[str, object]:
        return {
            "llm_adapter": self.llm_adapter,
            "tts_adapter": self.tts_adapter,
            "llm_model": self.llm_model,
            "tts_model": self.tts_model,
            "tts_voice": self.tts_voice,
            "tts_speech_rate": self.tts_speech_rate,
            "enable_thinking": self.enable_thinking,
            "has_api_key": bool(self.api_key),
            "workspace_configured": bool(self.workspace_id),
            "llm_base": self.resolved_llm_base,
            "tts_ws": self.resolved_tts_ws,
        }


def load_pipeline_settings() -> PipelineSettings:
    try:
        from dotenv import load_dotenv

        load_dotenv(_root() / ".env")
    except ImportError:
        pass
    data = _load_json()
    file_cfg = data.get("pipeline") or {}
    llm_cfg = file_cfg.get("llm") or {}
    tts_cfg = file_cfg.get("tts") or {}

    workspace = (
        os.getenv("PIPELINE_WORKSPACE_ID")
        or os.getenv("DASHSCOPE_WORKSPACE_ID")
        or file_cfg.get("workspace_id")
        or ""
    ).strip()
    api_key = (
        os.getenv("PIPELINE_API_KEY")
        or os.getenv("DASHSCOPE_API_KEY")
        or (llm_cfg.get("api_key") or tts_cfg.get("api_key") or file_cfg.get("api_key") or "")
    ).strip()
    thinking_default = bool(llm_cfg.get("enable_thinking", False))
    return PipelineSettings(
        llm_adapter=(
            os.getenv("PIPELINE_LLM_ADAPTER") or llm_cfg.get("adapter") or "dashscope_responses"
        ).strip().lower(),
        tts_adapter=(
            os.getenv("PIPELINE_TTS_ADAPTER") or tts_cfg.get("adapter") or "dashscope_tts"
        ).strip().lower(),
        api_key=api_key,
        workspace_id=workspace,
        llm_base_url=(os.getenv("PIPELINE_LLM_BASE_URL") or llm_cfg.get("base_url") or "").strip(),
        llm_model=(
            os.getenv("PIPELINE_LLM_MODEL") or llm_cfg.get("model") or DEFAULT_LLM_MODEL
        ).strip(),
        enable_thinking=_truthy(os.getenv("PIPELINE_ENABLE_THINKING"), thinking_default),
        tts_ws_url=(os.getenv("PIPELINE_TTS_WS_URL") or tts_cfg.get("ws_url") or "").strip(),
        tts_model=(
            os.getenv("PIPELINE_TTS_MODEL") or tts_cfg.get("model") or DEFAULT_TTS_MODEL
        ).strip(),
        tts_voice=(
            os.getenv("PIPELINE_TTS_VOICE") or tts_cfg.get("voice") or DEFAULT_TTS_VOICE
        ).strip(),
        tts_sample_rate=int(tts_cfg.get("sample_rate") or 24000),
        tts_speech_rate=_clamp_speech_rate(
            os.getenv("PIPELINE_TTS_SPEECH_RATE") or tts_cfg.get("speech_rate") or 1.2
        ),
    )


def build_streaming_llm(settings: PipelineSettings | None = None) -> StreamingLlm:
    cfg = settings or load_pipeline_settings()
    if cfg.llm_adapter in {"stub", "llm_stub"}:
        return StubLlm(chunk_delay_sec=0.012)
    from backend.pipeline.adapters.llm_dashscope import DashScopeResponsesLlm

    return DashScopeResponsesLlm(
        api_key=cfg.api_key,
        base_url=cfg.resolved_llm_base,
        model=cfg.llm_model,
        enable_thinking=cfg.enable_thinking,
    )


def build_streaming_tts(settings: PipelineSettings | None = None) -> StreamingTts:
    cfg = settings or load_pipeline_settings()
    if cfg.tts_adapter in {"stub", "tts_stub"}:
        return StubTts()
    from backend.pipeline.adapters.tts_dashscope import DashScopeSpeechTts

    return DashScopeSpeechTts(
        api_key=cfg.api_key,
        ws_url=cfg.resolved_tts_ws,
        model=cfg.tts_model,
        voice=cfg.tts_voice,
        sample_rate=cfg.tts_sample_rate,
        speech_rate=cfg.tts_speech_rate,
    )
