# -*- coding: utf-8 -*-
"""Outbound call task models for partner API."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


# Task lifecycle
STATUS_QUEUED = "queued"
STATUS_DIALING = "dialing"
STATUS_IN_CALL = "in_call"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"

# Business result labels (催授权)
RESULT_COMPLETED_AUTH_PENDING = "completed_auth_pending"
RESULT_ALREADY_DONE = "already_done"
RESULT_RESCHEDULE = "reschedule"
RESULT_ABANDONED = "abandoned"
RESULT_UNREACHABLE_ASSIST = "unreachable_assist"
RESULT_REFUSED = "refused"
RESULT_NO_ANSWER = "no_answer"
RESULT_BUSY = "busy"
RESULT_FAILED = "call_failed"


@dataclass
class CallTask:
    call_id: str
    scenario: str
    phone: str
    status: str = STATUS_QUEUED
    external_id: str = ""
    callback_url: str = ""
    payload: dict[str, Any] = field(default_factory=dict)
    system_role: str = ""
    speaking_style: str = ""
    telephony: dict[str, Any] = field(default_factory=dict)
    result: str | None = None
    result_summary: str = ""
    transcript: list[dict[str, str]] = field(default_factory=list)
    error: str = ""
    dry_run: bool = False
    created_at: str = field(default_factory=_utcnow)
    updated_at: str = field(default_factory=_utcnow)
    finished_at: str | None = None

    def touch(self) -> None:
        self.updated_at = _utcnow()

    def to_public_dict(self) -> dict[str, Any]:
        """Shape returned to partner (poll / webhook)."""
        return {
            "call_id": self.call_id,
            "external_id": self.external_id or None,
            "scenario": self.scenario,
            "phone": self.phone,
            "status": self.status,
            "result": self.result,
            "result_summary": self.result_summary or None,
            "transcript": self.transcript,
            "telephony": self.telephony or None,
            "error": self.error or None,
            "dry_run": self.dry_run,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "finished_at": self.finished_at,
        }

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
