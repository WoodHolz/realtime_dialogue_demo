# -*- coding: utf-8 -*-
"""Policy: call outcome → NextAction (or none)."""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from backend.prod.harness.context import build_callback_context
from backend.prod.harness.models import (
    ACTION_CALLBACK_RESCHEDULE,
    ACTION_RETRY_NO_ANSWER,
)
from backend.prod.paths import env_first

LOG = logging.getLogger(__name__)
BJ = ZoneInfo("Asia/Shanghai")

RETRY_DELAY_SEC = int(env_first("PROD_RETRY_SEC", "TMP_PROD_RETRY_SEC", default="1800"))
MAX_ATTEMPTS = 2

# MVP: only not_convenient (+ locked hour-level time) schedules a callback.
RESCHEDULE_RESULTS = frozenset({"not_convenient"})

_YMD_HM_RE = re.compile(
    r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日"
    r"(?:\s*[^\d]{0,6})?(\d{1,2})\s*[:：]\s*(\d{2})"
)


@dataclass
class PlannedAction:
    type: str
    due_at: datetime  # UTC
    reason: str
    callback_context: dict[str, Any]
    contact_status: str  # waiting_retry | waiting_callback


def promised_display_to_due_utc(display: str) -> datetime | None:
    """Parse absolute display like ``2026年7月24日 12:00`` → UTC due instant.

    Day-only (no hour) returns None — cannot schedule a concrete callback.
    """
    text = (display or "").strip()
    if not text:
        return None
    m = _YMD_HM_RE.search(text)
    if not m:
        return None
    y, mo, d, h, mi = (int(x) for x in m.groups())
    try:
        local = datetime(y, mo, d, h, mi, tzinfo=BJ)
    except ValueError:
        return None
    return local.astimezone(timezone.utc)


def plan_next_action(
    *,
    outcome: dict[str, Any],
    attempt: int,
    max_attempts: int = MAX_ATTEMPTS,
    is_no_answer: bool = False,
    source_call_id: str = "",
    contact_email: str = "",
    now: datetime | None = None,
    retry_delay_sec: int | None = None,
) -> PlannedAction | None:
    """Decide durable next work after a call finishes. None = terminal for harness."""
    now = now or datetime.now(timezone.utc)
    delay = RETRY_DELAY_SEC if retry_delay_sec is None else int(retry_delay_sec)

    if is_no_answer:
        if attempt < max_attempts:
            return PlannedAction(
                type=ACTION_RETRY_NO_ANSWER,
                due_at=now + timedelta(seconds=delay),
                reason="未接通，将按间隔重试",
                callback_context={},
                contact_status="waiting_retry",
            )
        return None

    result = str(outcome.get("result") or "").strip()
    promised = str(outcome.get("promised_time") or "").strip()

    if result in RESCHEDULE_RESULTS:
        due = promised_display_to_due_utc(promised)
        if due is None:
            LOG.info(
                "prod harness skip reschedule: no hour-level promised_time result=%s promised=%r",
                result,
                promised,
            )
            return None
        if due <= now:
            # Already past — call back soon (small grace) rather than drop.
            due = now + timedelta(seconds=30)
        ctx = build_callback_context(
            outcome=outcome,
            source_call_id=source_call_id,
            contact_email=contact_email,
        )
        return PlannedAction(
            type=ACTION_CALLBACK_RESCHEDULE,
            due_at=due,
            reason=f"改约回电：{promised}",
            callback_context=ctx,
            contact_status="waiting_callback",
        )

    return None
