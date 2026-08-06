# -*- coding: utf-8 -*-
"""After call completion: emit IA-03A / 03B / 05 to the platform."""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from backend.ia.models import ArtifactUploadMeta, CallEventRequest, CallOutcomeRequest
from backend.ia.platform_client import get_platform_client
from backend.ia.recording_upload import prepare_recording_for_upload
from backend.outbound.models import CallTask

LOG = logging.getLogger(__name__)
_TZ_SHANGHAI = ZoneInfo("Asia/Shanghai")

# Follow-up / callback miss → customer decides; no auto NO_ANSWER_RETRY.
_CUSTOMER_DECISION_DISPATCH = frozenset({"PROMISE_FOLLOW_UP", "CALLBACK_APPOINTMENT"})

# Internal xlsx display form → platform ISO datetime.
_CN_YMD_HM_RE = re.compile(
    r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日(?:\s+(\d{1,2}):(\d{2}))?"
)


def _tenant_id(ia: dict[str, Any]) -> int:
    """Platform requires tenantId on 03A/03B/05; raise if missing."""
    raw = ia.get("tenantId")
    if raw is None or raw == "":
        raise ValueError("IA payload missing tenantId (required by platform)")
    try:
        return int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"IA tenantId not int-compatible: {raw!r}") from exc


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


def _to_platform_datetime(raw: str | None) -> str | None:
    """Convert internal display time / ISO to platform datetime (+08:00).

    Internal outcome uses forms like ``2026年8月4日 18:00``; IA-03B requires
    ``2026-08-04T18:00:00+08:00``. Unparseable values are dropped (null).
    """
    text = str(raw or "").strip()
    if not text:
        return None
    if "T" in text or text.endswith("Z") or "+" in text[10:]:
        try:
            return _iso_offset(text)
        except Exception:
            pass
    m = _CN_YMD_HM_RE.fullmatch(text) or _CN_YMD_HM_RE.search(text)
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        h = int(m.group(4) or 0)
        mi = int(m.group(5) or 0)
        try:
            dt = datetime(y, mo, d, h, mi, tzinfo=_TZ_SHANGHAI)
            return dt.isoformat(timespec="seconds")
        except ValueError:
            LOG.warning("IA invalid Chinese datetime %r", text)
            return None
    LOG.warning("IA cannot map promised time to ISO: %r", text)
    return None


def _result_bundle_id(task: CallTask, ia: dict[str, Any]) -> str:
    existing = ia.get("resultBundleId")
    if existing:
        return str(existing)
    return f"rb{_call_id_int(task, ia)}"


def _execution_sequence(ia: dict[str, Any], *, default: int = 1) -> int:
    """Platform: executionSequence 不能为空; never omit."""
    raw = ia.get("executionSequence")
    if raw is None or raw == "":
        return default
    try:
        seq = int(raw)
        return seq if seq >= 1 else default
    except (TypeError, ValueError):
        LOG.warning("IA executionSequence not int-compatible: %r", raw)
        return default


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


_TRIGGER_FAMILY_RULES: tuple[tuple[tuple[str, ...], str], ...] = (
    (("开会", "会议", "meeting", "in a meeting"), "BUSY_MEETING"),
    (("开车", "驾驶", "driving"), "DRIVING"),
    (("信号", "signal"), "SIGNAL_BAD"),
    (("医院", "急诊", "emergency"), "EMERGENCY"),
    (("条件", "材料不齐", "unavailable"), "CONDITION_UNAVAILABLE"),
    (("组织", "领导", "审批", "org"), "ORG_NODE"),
    (("邮件", "短信", "稍后处理", "自己弄", "async"), "ASYNC_ACTION"),
    (("晚点", "待会", "一会儿", "稍后", "下午", "晚上", "改天", "有空", "方便", "later"), "VAGUE_LATER"),
)

_VAGUE_HINT_RE = re.compile(
    r"晚点|待会|一会儿|稍后|下午|晚上|改天|有空|方便时|sometime|later|soon",
    re.IGNORECASE,
)


def _infer_trigger_family(*parts: str | None) -> str | None:
    blob = " ".join(str(p) for p in parts if p).strip()
    if not blob:
        return None
    low = blob.lower()
    for keys, family in _TRIGGER_FAMILY_RULES:
        if any(k.lower() in low for k in keys):
            return family
    return None


def _empty_reschedule(*, summary: str | None = None) -> dict[str, Any]:
    return {
        "reason": None,
        "triggerFamily": None,
        "nextAction": {
            "type": "NONE",
            "channel": None,
            "owner": None,
            "dueTime": None,
            "dueWindow": None,
        },
        "weakCommitment": None,
        "lastSpokenSummary": summary,
    }


def _build_callback_reschedule(
    task: CallTask,
    *,
    promised_raw: str | None,
    biz_result: str | None,
) -> dict[str, Any]:
    """IA-03B §3.2 reschedule for recipient callback / 改约."""
    raw = (promised_raw or "").strip() or None
    due = _to_platform_datetime(raw) if raw else None
    summary = (task.result_summary or "").strip() or None
    family = _infer_trigger_family(summary, raw, biz_result, task.result)
    vague = bool(raw and _VAGUE_HINT_RE.search(raw)) or (bool(raw) and due is None)
    # Clear clock → firm; vague / unparseable → weak; no time at all but 改约 intent → weak.
    if due and not vague:
        weak: bool | None = False
        due_window = None
    elif raw:
        weak = True
        due_window = raw
        family = family or "VAGUE_LATER"
    else:
        weak = True
        due_window = None
        family = family or "VAGUE_LATER"

    return {
        "reason": "RECIPIENT_CALLBACK_REQUEST",
        "triggerFamily": family,
        "nextAction": {
            "type": "CALLBACK",
            "channel": "VOICE",
            "owner": "PLATFORM",
            "dueTime": due,
            "dueWindow": due_window,
        },
        "weakCommitment": weak,
        "lastSpokenSummary": summary or (f"候选人改约回电：{raw}" if raw else "候选人要求改约回电"),
    }


def _is_reschedule_intent(task: CallTask, biz: dict[str, Any]) -> bool:
    result = (task.result or biz.get("result") or "").strip()
    if result in {"not_convenient", "reschedule"}:
        return True
    if (biz.get("promised_time") or "").strip():
        return True
    return False


def _dial_failure_reason(task: CallTask, call_outcome: str) -> str | None:
    """Human-readable dial failure for IA-03A/03B (≤500 chars, zh+en).

    Never paste raw SIP/provider error strings into the payload — only classify
    soft signals to pick a natural-language description.
    """
    if call_outcome not in {
        "FAILED",
        "NO_ANSWER",
        "BUSY",
        "INVALID_NUMBER",
        "ASSISTANT_BLOCKED",
    }:
        return None

    # Soft signals for classification only (never copied into the returned text).
    blob = " ".join(
        str(x)
        for x in (
            task.result,
            task.error,
            task.result_summary,
            ((task.telephony or {}).get("sip") or {}).get("status"),
            ((task.telephony or {}).get("sip") or {}).get("hangup_reason"),
            ((task.telephony or {}).get("sip") or {}).get("error"),
        )
        if x
    ).lower()

    if call_outcome == "NO_ANSWER":
        text = (
            "被叫未接听，振铃后无应答或线路侧返回未接通。"
            " The callee did not answer; the call rang out or the carrier reported no answer."
        )
    elif call_outcome == "BUSY":
        text = (
            "被叫占线，当前无法接通。"
            " The callee line was busy and could not be connected."
        )
    elif call_outcome == "ASSISTANT_BLOCKED":
        text = (
            "疑似语音助手代接或来电拦截，未能进入有效人工对话。"
            " Likely answered by a voice assistant or call blocker; no effective human conversation."
        )
    elif call_outcome == "INVALID_NUMBER":
        text = (
            "号码无效或不可达，无法建立呼叫。"
            " The number is invalid or unreachable; the call could not be established."
        )
    elif "concurrency" in blob or "并发" in blob:
        text = (
            "外呼并发已达上限，本次未能发起拨号。"
            " Outbound concurrency limit reached; this dial attempt was not started."
        )
    elif any(k in blob for k in ("480", "486", "487", "no answer", "no_answer", "未接")):
        text = (
            "多次或单次拨号后仍未接通，被叫侧无有效应答。"
            " Dialing completed without a successful connect; no effective answer from the callee."
        )
    elif any(
        k in blob
        for k in (
            "normal_clearing",
            "cause=16",
            "bye",
            "hangup",
            "cleared",
        )
    ):
        text = (
            "呼叫过程中线路异常结束或对端提前释放，未能完成有效通话。"
            " The call ended abnormally or the peer released early before an effective conversation."
        )
    elif any(k in blob for k in ("404", "not found", "unreachable", "不可达")):
        text = (
            "被叫暂时不可达或路由失败，呼叫未能接通。"
            " The callee was temporarily unreachable or routing failed; the call did not connect."
        )
    elif any(k in blob for k in ("rtp", "sdp", "connect failed", "bind", "timeout", "超时")):
        text = (
            "媒体或信令链路建立失败，系统侧未能完成通话连接。"
            " Media or signaling setup failed; the system could not complete the call connection."
        )
    elif any(k in blob for k in ("credential", "doubao", "auth", "鉴权")):
        text = (
            "语音服务鉴权或配置异常，导致本次外呼未能正常进行。"
            " Voice service auth or configuration failed, so this outbound call could not proceed."
        )
    else:
        text = (
            "系统侧呼叫失败，未能建立有效通话（非业务改约/未接语义）。"
            " System-side dial failure; no effective call was established "
            "(not a business no-answer or reschedule case)."
        )

    return text[:500]


def build_call_event(
    task: CallTask,
    *,
    execution_sequence: int | None = None,
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
    seq = (
        int(execution_sequence)
        if execution_sequence is not None
        else _execution_sequence(ia)
    )
    return CallEventRequest(
        taskId=int(ia["taskId"]),
        callId=_call_id_int(task, ia),
        tenantId=_tenant_id(ia),
        dispatchId=str(dispatch_id) if dispatch_id else None,
        taskRevision=int(ia.get("taskRevision") or 1),
        executionSequence=seq,
        resultBundleId=bundle_id,
        eventType="CALL_FINISHED",
        occurredAt=occurred,
        # Smoke: attemptNo unused by both sides; omit from payload.
        attemptNo=None,
        attemptCounted=attempt_counted,
        callOutcome=call_outcome,
        hangupInitiator="UNKNOWN",
        dialFailureReason=_dial_failure_reason(task, call_outcome),
        startedAt=started,
        endedAt=ended,
        durationSeconds=duration,
        scriptVersion=str(ia.get("scriptVersion") or "2.3"),
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
    biz = ((task.telephony or {}).get("business") or {}) if task.telephony else {}

    feedback: list[dict[str, Any]] = []
    reschedule: dict[str, Any]

    # Follow-up / callback miss → customer decision; no auto NO_ANSWER_RETRY.
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
            "lastSpokenSummary": (task.result_summary or None),
        }
        # feedbackItems = client actionable tickets only; 人未接不是工单，保持 [].
        # 未接事实在 03A callOutcome；是否继续联系由平台/客户看 §3.2 NONE.
    elif call_outcome in {"NO_ANSWER", "BUSY"}:
        # Business unreachable → platform may schedule NO_ANSWER_RETRY (dueTime by platform).
        reschedule = {
            "reason": "NO_ANSWER_RETRY",
            "triggerFamily": None,
            "nextAction": {
                "type": "CALLBACK",
                "channel": "VOICE",
                "owner": "PLATFORM",
                "dueTime": None,
                "dueWindow": None,
            },
            "weakCommitment": False,
            "lastSpokenSummary": (task.result_summary or "未接通，建议无人接听重呼"),
        }
    elif _is_reschedule_intent(task, biz):
        # 改约 / 稍后回拨 → IA-03B §3.2 RECIPIENT_CALLBACK_REQUEST
        reschedule = _build_callback_reschedule(
            task,
            promised_raw=(biz.get("promised_time") or None),
            biz_result=str(biz.get("result") or task.result or ""),
        )
    elif call_outcome == "FAILED":
        # System dial failure: dialFailureReason lives on IA-03A, not feedbackItems.
        reschedule = _empty_reschedule(summary=None)
    else:
        reschedule = _empty_reschedule(summary=(task.result_summary or None))

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
        tenantId=_tenant_id(ia),
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
    )


async def report_ia_call_finished(task: CallTask) -> None:
    """Fire 03A → 03B → 05; failures are logged and do not raise to SIP path."""
    ia = _ia_meta(task)
    if not ia:
        return

    # Release INITIAL slot so platform can dispatch again without process restart.
    try:
        from backend.ia.store import get_dispatch_store

        get_dispatch_store().mark_terminal(int(ia["taskId"]))
    except Exception:
        LOG.exception("IA mark_terminal failed taskId=%s", ia.get("taskId"))

    client = get_platform_client()
    bundle_id = _result_bundle_id(task, ia)
    produced_at = _iso_offset()
    ia_call_id = _call_id_int(task, ia)

    try:
        event = build_call_event(task, result_bundle_id=bundle_id)
        LOG.info(
            "IA-03A posting taskId=%s iaCallId=%s tenantId=%s executionSequence=%s bundle=%s",
            event.taskId,
            event.callId,
            event.tenantId,
            event.executionSequence,
            event.resultBundleId,
        )
        await client.post_call_event(event)
    except Exception:
        LOG.exception(
            "IA-03A report failed call_id=%s iaCallId=%s",
            task.call_id,
            ia_call_id,
        )

    try:
        outcome = build_call_outcome(
            task,
            result_bundle_id=bundle_id,
            produced_at=produced_at,
        )
        LOG.info(
            "IA-03B posting taskId=%s iaCallId=%s tenantId=%s bundle=%s",
            outcome.taskId,
            outcome.callId,
            outcome.tenantId,
            outcome.resultBundleId,
        )
        await client.post_call_outcome(outcome)
    except Exception:
        LOG.exception(
            "IA-03B report failed call_id=%s iaCallId=%s",
            task.call_id,
            ia_call_id,
        )

    # IA-05 merged upload (best-effort; must not block 03A/03B success path conceptually)
    try:
        sip = (task.telephony or {}).get("sip") or {}
        recordings = sip.get("recordings") or {}
        stereo = recordings.get("stereo")
        if stereo and Path(stereo).is_file():
            upload_path = prepare_recording_for_upload(Path(stereo))
            meta = ArtifactUploadMeta(
                taskId=int(ia["taskId"]),
                callId=ia_call_id,
                artifactType="RECORDING",
                productAt=_iso_offset(task.finished_at),
                tenantId=_tenant_id(ia),
            )
            await client.upload_recording(meta=meta, file_path=upload_path)
    except Exception:
        LOG.exception(
            "IA-05 upload failed call_id=%s iaCallId=%s",
            task.call_id,
            ia_call_id,
        )
