# -*- coding: utf-8 -*-
"""Map IA-01 camelCase dispatch → outbound create_outbound_call kwargs."""
from __future__ import annotations

from typing import Any

from backend.ia.models import TaskDispatchRequest

# Internal dial scenario that drives prod_prompt (芝麻背调催授权).
INTERNAL_SCENARIO = "prod_auth_demo"


def dispatch_to_outbound_payload(req: TaskDispatchRequest) -> dict[str, Any]:
    """Flatten IA slots/identity/context into existing prompt payload keys."""
    ident = req.identity
    slots = req.slots
    ctx = req.context

    identity_mode = ident.mode
    if identity_mode == "CLIENT_IDENTITY":
        client_name = (ident.customerBrandName or "").strip()
        our_brand = ""
        employer = client_name
    else:
        client_name = (ident.clientBuName or "").strip()
        our_brand = (ident.ourBrandName or "").strip()
        employer = client_name

    callback_context: dict[str, Any] = {}
    if ctx.lastSpokenSummary:
        callback_context["last_spoken_summary"] = ctx.lastSpokenSummary
    if ctx.recipientPromisedTime:
        callback_context["promised_time"] = ctx.recipientPromisedTime
    if ctx.lastTriggerFamily:
        callback_context["trigger_family"] = ctx.lastTriggerFamily
    if ctx.clientNoteFiltered:
        callback_context["client_note"] = ctx.clientNoteFiltered

    call_kind = ""
    if req.dispatchType == "PROMISE_FOLLOW_UP":
        call_kind = "reschedule"
    elif req.dispatchType == "CALLBACK_APPOINTMENT":
        call_kind = "reschedule"

    channels = slots.channels
    order = slots.order
    contact = slots.contact

    return {
        "candidate_name": req.recipient.name,
        "phone": req.recipient.mobile,
        "email": req.recipient.email or "",
        "identity_mode": identity_mode,
        "client_name": client_name,
        "our_brand_name": our_brand,
        "employer": employer,
        "callback_number": contact.callbackNumber,
        "working_hours": contact.workingHours or "09:00-18:00",
        "has_email": bool(channels.emailEnabled),
        "fallback_sms_enabled": bool(channels.fallbackSmsEnabled),
        "fallback_sms_body": channels.fallbackSmsContent or "",
        "mp_name": channels.publicAccountName or "",
        "sms_prefix": channels.smsTextPrefix or "",
        "email_subject": channels.emailSubject or "",
        "work_order_no": slots.workOrderNo or "",
        "order_type": order.orderType,
        "authorization_status": order.authorizationStatus,
        "order_deadline": order.deadline or "",
        "call_kind": call_kind,
        "callback_context": callback_context,
        "prior_promised_time": ctx.recipientPromisedTime or "",
        # IA bookkeeping for reporter
        "ia": {
            "dispatchId": req.dispatchId,
            "taskId": req.taskId,
            "taskRevision": req.taskRevision,
            "dispatchType": req.dispatchType,
            "externalOrderId": req.externalOrderId,
            "scriptId": req.scriptId,
            "scriptVersion": req.scriptVersion,
            "scenario": req.scenario,
            "tenantId": req.tenantId,
            "maxAttempts": req.retryPolicy.maxAttempts,
            "retryIntervalMinutes": req.retryPolicy.retryIntervalMinutes,
            "timezone": req.timezone or "Asia/Shanghai",
            "callWindow": req.callWindow,
        },
    }


def estimate_planned_call_at(
    *,
    timezone_name: str = "Asia/Shanghai",
    call_window: str = "09:00-21:00",
    requested_at: str | None = None,
) -> str:
    """Return ISO8601 with offset; never null (IA-01 contract)."""
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo

    try:
        tz = ZoneInfo(timezone_name or "Asia/Shanghai")
    except Exception:
        tz = ZoneInfo("Asia/Shanghai")

    now = datetime.now(tz)
    if requested_at:
        try:
            raw = requested_at.replace("Z", "+00:00")
            dt = datetime.fromisoformat(raw)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=tz)
            else:
                dt = dt.astimezone(tz)
            if dt > now:
                now = dt
        except ValueError:
            pass

    start_s, end_s = "09:00", "21:00"
    parts = (call_window or "09:00-21:00").split("-")
    if len(parts) == 2:
        start_s, end_s = parts[0].strip(), parts[1].strip()

    def _hm(text: str) -> tuple[int, int]:
        hh, mm = text.split(":")[:2]
        return int(hh), int(mm)

    sh, sm = _hm(start_s)
    eh, em = _hm(end_s)
    window_start = now.replace(hour=sh, minute=sm, second=0, microsecond=0)
    window_end = now.replace(hour=eh, minute=em, second=0, microsecond=0)

    # Small buffer so "asap" is slightly in the future.
    candidate = now + timedelta(seconds=30)
    if candidate < window_start:
        candidate = window_start
    elif candidate >= window_end:
        nxt = window_start + timedelta(days=1)
        candidate = nxt

    return candidate.isoformat()
