# -*- coding: utf-8 -*-
"""tmp-prod harness NextAction model (out-of-call orchestration)."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timezone
from typing import Any


ACTION_RETRY_NO_ANSWER = "retry_no_answer"
ACTION_CALLBACK_RESCHEDULE = "callback_reschedule"

STATUS_PENDING = "pending"
STATUS_RUNNING = "running"
STATUS_DONE = "done"
STATUS_CANCELLED = "cancelled"
STATUS_SKIPPED = "skipped"

CALL_KIND_INITIAL = "initial"
CALL_KIND_RESCHEDULE = "reschedule_callback"


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class NextAction:
    """Durable scheduled work: due_at on disk, no long-lived sleep task."""

    action_id: str
    batch_id: str
    contact_id: str
    type: str  # retry_no_answer | callback_reschedule
    due_at: str  # ISO UTC
    status: str = STATUS_PENDING
    reason: str = ""
    source_call_id: str = ""
    dial_options: dict[str, Any] = field(default_factory=dict)
    callback_context: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=_utcnow)
    updated_at: str = field(default_factory=_utcnow)
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def action_from_dict(data: dict[str, Any] | None) -> NextAction | None:
    if not data:
        return None
    known = {f.name for f in fields(NextAction)}
    kwargs = {k: v for k, v in data.items() if k in known}
    if not kwargs.get("action_id"):
        return None
    return NextAction(**kwargs)
