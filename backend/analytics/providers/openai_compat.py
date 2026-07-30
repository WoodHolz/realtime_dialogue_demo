# -*- coding: utf-8 -*-
"""OpenAI-compatible chat/responses usage adapter (follow-up LLM, etc.)."""
from __future__ import annotations

import os
from typing import Any

from backend.analytics.models import ProviderDayUsage, UsageDelta

PROVIDER_ID = "openai_compat"


def extract_openai_usage(data: dict[str, Any] | None) -> dict[str, float]:
    """Normalize OpenAI-style usage object to standard keys."""
    if not isinstance(data, dict):
        return {}
    usage = data.get("usage") if "usage" in data else data
    if not isinstance(usage, dict):
        return {}
    out: dict[str, float] = {}
    mapping = {
        "input_tokens": ("input_tokens", "prompt_tokens"),
        "output_tokens": ("output_tokens", "completion_tokens"),
        "total_tokens": ("total_tokens",),
    }
    for std, keys in mapping.items():
        for k in keys:
            v = usage.get(k)
            if isinstance(v, (int, float)):
                out[std] = float(v)
                break
    if "total_tokens" not in out:
        total = out.get("input_tokens", 0.0) + out.get("output_tokens", 0.0)
        if total:
            out["total_tokens"] = total
    return out


class OpenAICompatUsageAdapter:
    provider_id = PROVIDER_ID

    def on_session_event(self, event: dict[str, Any]) -> list[UsageDelta]:
        # Not used for streaming WS; follow-up uses extract_openai_usage + record_component_usage.
        if not isinstance(event, dict):
            return []
        metrics = extract_openai_usage(event)
        if not metrics:
            return []
        return [UsageDelta(metrics=metrics, event_type=str(event.get("type") or "openai_usage"))]

    async def pull_day_usage(self, day: str) -> ProviderDayUsage:
        # Most OpenAI-compat providers have no unified day-pull in this demo.
        # Explicit opt-in placeholder; default skipped.
        if (os.environ.get("OPENAI_COMPAT_USAGE_PULL") or "").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }:
            return ProviderDayUsage(
                provider_id=self.provider_id,
                day=day,
                status="error",
                error="OPENAI_COMPAT_USAGE_PULL set but no provider pull URL configured",
            )
        return ProviderDayUsage(
            provider_id=self.provider_id,
            day=day,
            status="skipped",
            error="no day-pull configured for openai_compat",
        )
