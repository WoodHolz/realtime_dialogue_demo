# -*- coding: utf-8 -*-
"""After call completion: emit IA-03A / 03B / 05 to the platform."""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from backend.ia.models import ArtifactUploadMeta, CallEventRequest, CallOutcomeRequest
from backend.ia.platform_client import get_platform_client
from backend.outbound.models import CallTask

LOG = logging.getLogger(__name__)
_TZ_SHANGHAI = ZoneInfo("Asia/Shanghai")

# Follow-up / callback miss → customer decides; no auto NO_ANSWER_RETRY.
_CUSTOMER_DECISION_DISPATCH = frozenset({"PROMISE_FOLLOW_UP", "CALLBACK_APPOINTMENT"})


def _tenant_id(ia: dict[str, Any]) -> int | None:
    raw = ia.get("tenantId")
    if raw is None or raw == "":
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        LOG.warning("IA tenantId not int-compatible: %r", raw)
        return None


def _ia_meta(task: CallTask) -> dict[str, Any] | None:
    raw = (task.payload or {}).get("ia")
    return dict(raw) if isinstance(raw, dict) and raw.get("taskId") is not None else None


def _call_id_int(task: CallTask, ia: dict[str, Any]) -> int:
    """Prefer platform long callId from IA payload; never use internal uuid hex."""
    if ia.get("callId") is not None:
        return int(ia["callId"])
    raise ValueError(f"IA payload missing numeric callId for task.call_id={task.call_id!r}")


def _iso_offset(value: str | None = None) -> str:
    """ISO 8601 datetime with UTC offset (doc: algo in CN → +08:00)."""
    if value:
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(_TZ_SHANGHAI).isoformat(timespec="seconds")
        except Exception:
            pass
    return datetime.now(_TZ_SHANGHAI).isoformat(timespec="seconds")


def _result_bundle_id(task: CallTask, ia: dict[str, Any]) -> str:
    existing = ia.get("resultBundleId")
    if existing:
        return str(existing)
    return f"rb{_call_id_int(task, ia)}"


def _attempt_no(ia: dict[str, Any]) -> int | None:
    raw = ia.get("attemptNo")
    if raw is None or raw == "":
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        LOG.warning("IA attemptNo not int-compatible: %r", raw)
        return None


def _map_call_outcome(task: CallTask) -> tuple[str, bool]:
    """Return (callOutcome enum, attemptCounted)."""
    result = (task.result or "").strip()
    if result in {"no_answer"}:
        return "NO_ANSWER", True
    if result in {"busy"}:
        return "BUSY", True
    if result in {"call_failed"} or task.error:
        return "FAILED", False
    if result in {"unreachable_assist"}:
        return "ASSISTANT_BLOCKED", True
    # Connected-ish business outcomes
    if result:
        # Heuristic: abandoned/refused/reschedule/already_done/completed → connected
        if result in {"abandoned", "refused"}:
            return "CONNECTED_INEFFECTIVE", True
        return "CONNECTED_EFFECTIVE", True
    if task.error:
        return "FAILED", False
    return "CONNECTED_INEFFECTIVE", True


def build_call_event(
    task: CallTask,
    *,
    execution_sequence: int = 1,
    result_bundle_id: str | None = None,
) -> CallEventRequest:
    ia = _ia_meta(task) or {}
    call_outcome, attempt_counted = _map_call_outcome(task)
    started = _iso_offset(task.created_at)
    ended = _iso_offset(task.finished_at)
    occurred = ended
    duration = None
    try:
        if task.created_at and task.finished_at:
            t0 = datetime.fromisoformat(task.created_at.replace("Z", "+00:00"))
            t1 = datetime.fromisoformat(task.finished_at.replace("Z", "+00:00"))
            duration = max(0, int((t1 - t0).total_seconds()))
    except Exception:
        duration = None

    bundle_id = result_bundle_id or _result_bundle_id(task, ia)
    dispatch_id = ia.get("dispatchId")
    return CallEventRequest(
        taskId=int(ia["taskId"]),
        callId=_call_id_int(task, ia),
        dispatchId=str(dispatch_id) if dispatch_id else None,
        taskRevision=int(ia.get("taskRevision") or 1),
        executionSequence=execution_sequence,
        resultBundleId=bundle_id,
        eventType="CALL_FINISHED",
        occurredAt=occurred,
        attemptNo=_attempt_no(ia),
        attemptCounted=attempt_counted,
        callOutcome=call_outcome,
        hangupInitiator="UNKNOWN",
        startedAt=started,
        endedAt=ended,
        durationSeconds=duration,
        scriptVersion=str(ia.get("scriptVersion") or "2.3"),
        tenantId=_tenant_id(ia),
    )


def build_call_outcome(
    task: CallTask,
    *,
    result_bundle_id: str | None = None,
    produced_at: str | None = None,
) -> CallOutcomeRequest:
    ia = _ia_meta(task) or {}
    identity_mode = str((task.payload or {}).get("identity_mode") or "BRAND_SELF")
    call_outcome = _map_call_outcome(task)[0]
    connected_effective = call_outcome == "CONNECTED_EFFECTIVE"
    dispatch_type = str(ia.get("dispatchType") or "INITIAL")
    bundle_id = result_bundle_id or _result_bundle_id(task, ia)

    # Follow-up / callback missed → customer decision, no auto retry.
    feedback: list[dict[str, Any]] = []
    reschedule: dict[str, Any]
    if dispatch_type in _CUSTOMER_DECISION_DISPATCH and call_outcome in {
        "NO_ANSWER",
        "BUSY",
        "FAILED",
    }:
        reschedule = {
            "reason": None,
            "triggerFamily": None,
            "nextAction": {
                "type": "NONE",
                "channel": None,
                "owner": "CLIENT",
                "dueTime": None,
                "dueWindow": None,
            },
            "weakCommitment": None,
            "lastSpokenSummary": None,
        }
        feedback.append(
            {
                "type": "CONTACT_PREFERENCE",
                "action": "NONE",
                "priority": "P1",
                "reasonCode": "FOLLOW_UP_UNREACHED",
                "summary": "跟进/回拨未触达，是否继续联系请客户决策",
            }
        )
    else:
        # Prefer prod outcome extraction if present on telephony.business
        biz = ((task.telephony or {}).get("business") or {}) if task.telephony else {}
        promised = biz.get("promised_time") or None
        reschedule = {
            "reason": "RECIPIENT_CALLBACK_REQUEST" if promised else None,
            "triggerFamily": None,
            "nextAction": {
                "type": "CALLBACK" if promised else "NONE",
                "channel": "VOICE" if promised else None,
                "owner": "PLATFORM" if promised else None,
                "dueTime": promised,
                "dueWindow": None,
            },
            "weakCommitment": False if promised else None,
            "lastSpokenSummary": (task.result_summary or None),
        }

    compliance = {
        "identityMode": identity_mode if identity_mode in {"BRAND_SELF", "CLIENT_IDENTITY"} else "BRAND_SELF",
        "aiDisclosed": True if connected_effective else None,
        "entrustmentStated": (True if connected_effective else None)
        if identity_mode == "BRAND_SELF"
        else None,
        "identityStatementCompliant": True if connected_effective else None,
    }

    # resendRequested: true/false when connected dialog could judge; null when not connected.
    resend_requested: bool | None = False if connected_effective else None

    return CallOutcomeRequest(
        taskId=int(ia["taskId"]),
        callId=_call_id_int(task, ia),
        resultBundleId=bundle_id,
        scenario="AUTHORIZATION_REMINDER",
        scenarioSchemaVersion="1.0",
        producedAt=produced_at or _iso_offset(),
        compliance=compliance,
        recipientPromisedTime=reschedule.get("nextAction", {}).get("dueTime"),
        reschedule=reschedule,
        humanNeeded=False,
        recipientWithdrawn=False,
        feedbackItems=feedback,
        scenarioPayload={
            "channelSwitched": None,
            "deliveryCheck": {
                "sms": None,
                "email": "NOT_CONFIGURED",
                "fallbackSms": None,
            },
            "mpGuided": None,
            "resendRequested": resend_requested,
            "resendChannel": None,
            "anomalyTags": [],
            "trustChallenge": None,
        },
        tenantId=_tenant_id(ia),
    )


async def report_ia_call_finished(task: CallTask) -> None:
    """Fire 03A → 03B → 05; failures are logged and do not raise to SIP path."""
    ia = _ia_meta(task)
    if not ia:
        return

    client = get_platform_client()
    bundle_id = _result_bundle_id(task, ia)
    produced_at = _iso_offset()

    try:
        event = build_call_event(task, result_bundle_id=bundle_id)
        await client.post_call_event(event)
    except Exception:
        LOG.exception("IA-03A report failed call_id=%s", task.call_id)

    try:
        outcome = build_call_outcome(
            task,
            result_bundle_id=bundle_id,
            produced_at=produced_at,
        )
        await client.post_call_outcome(outcome)
    except Exception:
        LOG.exception("IA-03B report failed call_id=%s", task.call_id)

    # IA-05 merged upload (best-effort; must not block 03A/03B success path conceptually)
    try:
        sip = (task.telephony or {}).get("sip") or {}
        recordings = sip.get("recordings") or {}
        stereo = recordings.get("stereo")
        if stereo and Path(stereo).is_file():
            meta = ArtifactUploadMeta(
                taskId=int(ia["taskId"]),
                callId=_call_id_int(task, ia),
                artifactType="RECORDING",
                createdAt=_iso_offset(task.finished_at),
                tenantId=_tenant_id(ia),
            )
            await client.upload_recording(meta=meta, file_path=Path(stereo))
    except Exception:
        LOG.exception("IA-05 upload failed call_id=%s", task.call_id)
