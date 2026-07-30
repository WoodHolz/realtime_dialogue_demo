# -*- coding: utf-8 -*-
"""Provider usage adapters registry."""
from __future__ import annotations

from backend.analytics.providers.base import ProviderUsageAdapter
from backend.analytics.providers.doubao import DoubaoUsageAdapter
from backend.analytics.providers.openai_compat import OpenAICompatUsageAdapter


def get_adapters() -> list[ProviderUsageAdapter]:
    return [
        DoubaoUsageAdapter(),
        OpenAICompatUsageAdapter(),
    ]


def get_adapter(provider_id: str) -> ProviderUsageAdapter | None:
    for a in get_adapters():
        if a.provider_id == provider_id:
            return a
    return None
