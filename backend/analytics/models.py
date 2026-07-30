# -*- coding: utf-8 -*-
"""Analytics record shapes (provider-agnostic)."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


# Standard usage metric keys (fill what you have; never invent tokens).
STANDARD_USAGE_KEYS = (
    "input_tokens",
    "output_tokens",
    "total_tokens",
    "audio_in_sec",
    "audio_out_sec",
    "asr_chars",
    "tts_chars",
    "turns_user",
    "turns_assistant",
)


@dataclass
class CallAnalyticsRecord:
    call_id: str
    scenario: str = ""
    provider: str = ""
    model: str = ""
    started_at: str = ""
    ended_at: str = ""
    duration_sec: float = 0.0
    connected: bool = False
    hangup_reason: str = ""
    hangup_party: str = ""  # remote | local | system | unknown
    hangup_stage: str = ""
    hangup_stage_confidence: str = ""  # result | heuristic | reason_only | not_connected
    business_result: str = ""
    usage: dict[str, Any] = field(default_factory=dict)
    # usage.local / usage.provider_session / usage.components.{name}
    extras: dict[str, Any] = field(default_factory=dict)
    recorded_at: str = field(default_factory=_utcnow)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CallAnalyticsRecord:
        known = {f.name for f in cls.__dataclass_fields__.values()}  # type: ignore[attr-defined]
        kwargs = {k: v for k, v in data.items() if k in known}
        return cls(**kwargs)


@dataclass
class ProviderDayUsage:
    provider_id: str
    day: str
    status: str  # ok | skipped | error
    metrics: dict[str, Any] = field(default_factory=dict)
    raw: Any = None
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider_id": self.provider_id,
            "day": self.day,
            "status": self.status,
            "metrics": dict(self.metrics or {}),
            "raw": self.raw,
            "error": self.error or "",
        }


@dataclass
class UsageDelta:
    """Incremental provider-reported usage from a session event."""

    metrics: dict[str, float] = field(default_factory=dict)
    extras: dict[str, Any] = field(default_factory=dict)
    event_type: str = ""
