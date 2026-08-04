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
            "subjectKey": req.subjectKey,
            "maxAttempts": req.retryPolicy.maxAttempts,
            "retryIntervalMinutes": req.retryPolicy.retryIntervalMinutes,
            "timezone": req.timezone or "Asia/Shanghai",
            "callWindow": req.callWindow,
        },
    }
