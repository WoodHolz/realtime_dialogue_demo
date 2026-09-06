# -*- coding: utf-8 -*-
"""Shared LLM factory for bench runners (DashScope / MaaS Responses + Ark Chat Completions)."""
from __future__ import annotations

import os

from backend.pipeline.config import PipelineSettings
from bench.providers import (
    load_ark_api_key,
    load_ark_base_url,
    load_maas_api_key,
    load_maas_base_url,
    parse_model_spec,
)


def _truthy_env(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None or not str(raw).strip():
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def build_llm_for_model(settings: PipelineSettings, model: str):
    if settings.llm_adapter in {"stub", "llm_stub"}:
        from backend.pipeline.adapters.llm_stub import StubLlm

        return StubLlm(chunk_delay_sec=0.012)

    spec = parse_model_spec(model)
    if spec.provider == "stub":
        from backend.pipeline.adapters.llm_stub import StubLlm

        return StubLlm(chunk_delay_sec=0.012)

    if spec.provider == "ark":
        from backend.pipeline.adapters.llm_ark import ArkChatCompletionsLlm, resolve_ark_service_tier

        return ArkChatCompletionsLlm(
            api_key=load_ark_api_key(),
            base_url=load_ark_base_url(),
            model=spec.model,
            service_tier=resolve_ark_service_tier(),
        )

    from backend.pipeline.adapters.llm_dashscope import DashScopeResponsesLlm

    # Per-model +thinking wins; else BENCH_ENABLE_THINKING; else pipeline setting (dashscope only).
    if spec.enable_thinking:
        thinking = True
    elif _truthy_env("BENCH_ENABLE_THINKING", False):
        thinking = True
    elif spec.provider == "maas":
        thinking = _truthy_env("BENCH_MAAS_ENABLE_THINKING", False)
    else:
        thinking = bool(settings.enable_thinking)

    if spec.provider == "maas":
        # Default: same public DashScope host/key as qwen3.5-flash.
        # Only if BENCH_MAAS_BASE_URL is set do we hit a workspace endpoint.
        maas_base = load_maas_base_url()
        return DashScopeResponsesLlm(
            api_key=load_maas_api_key(settings),
            base_url=maas_base or settings.resolved_llm_base,
            model=spec.model,
            enable_thinking=thinking,
        )

    return DashScopeResponsesLlm(
        api_key=settings.api_key,
        base_url=settings.resolved_llm_base,
        model=spec.model,
        enable_thinking=thinking,
    )
