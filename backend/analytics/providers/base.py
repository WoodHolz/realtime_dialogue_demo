# -*- coding: utf-8 -*-
"""Provider usage adapter protocol."""
from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from backend.analytics.models import ProviderDayUsage, UsageDelta


@runtime_checkable
class ProviderUsageAdapter(Protocol):
    provider_id: str

    def on_session_event(self, event: dict[str, Any]) -> list[UsageDelta]:
        """Parse a realtime session event into usage deltas (may be empty)."""
        ...

    async def pull_day_usage(self, day: str) -> ProviderDayUsage:
        """Pull official account-level usage for a calendar day (YYYY-MM-DD)."""
        ...
