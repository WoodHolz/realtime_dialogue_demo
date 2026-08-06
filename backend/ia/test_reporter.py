# -*- coding: utf-8 -*-
"""IA reporter: 03A/03B/05 field alignment with 2026-07-30 docs."""
from __future__ import annotations

import pytest

from backend.ia.reporter import build_call_event, build_call_outcome, _tenant_id
from backend.ia.models import ArtifactUploadMeta
from backend.outbound.models import CallTask


def _task_with_ia(**ia_extra) -> CallTask:
    ia = {
        "taskId": 1948372615340032001,
        "callId": 10001,
        "taskRevision": 1,
        "tenantId": 10001,
        "dispatchId": "disp_test_001",
        "dispatchType": "INITIAL",
        "scriptVersion": "2.3",
        "allowAutoRetry": True,
        **ia_extra,
    }
    return CallTask(
        call_id="uuid-internal",
        scenario="prod_auth_demo",
        phone="13800138000",
        result="email_registered",
        result_summary="ok",
        payload={"ia": ia, "identity_mode": "BRAND_SELF"},
        created_at="2026-08-03T09:00:00+00:00",
        finished_at="2026-08-03T09:02:00+00:00",
    )


def test_tenant_id_coercion():
    assert _tenant_id({"tenantId": 10001}) == 10001
    assert _tenant_id({"tenantId": "10001"}) == 10001
    with pytest.raises(ValueError):
        _tenant_id({})
    with pytest.raises(ValueError):
        _tenant_id({"tenantId": None})


def test_03a_03b_require_tenant_id():
    task = _task_with_ia(tenantId=10001)
    event = build_call_event(task)
    outcome = build_call_outcome(task)
    assert event.tenantId == 10001
    assert outcome.tenantId == 10001
    dumped_e = event.model_dump(mode="json", exclude_none=True)
    dumped_o = outcome.model_dump(mode="json", exclude_none=True)
    assert dumped_e["tenantId"] == 10001
    assert dumped_o["tenantId"] == 10001
    assert dumped_e["executionSequence"] == 1
    assert "attemptNo" not in dumped_e


def test_03a_missing_tenant_id_raises():
    task = _task_with_ia()
    del task.payload["ia"]["tenantId"]
    with pytest.raises(ValueError, match="tenantId"):
        build_call_event(task)


def test_03a_required_fields_and_no_deprecated_status():
    task = _task_with_ia()
    event = build_call_event(task, result_bundle_id="rb_shared")
    dumped = event.model_dump(mode="json", exclude_none=True)
    assert dumped["occurredAt"].endswith("+08:00")
    assert dumped["resultBundleId"] == "rb_shared"
    assert dumped["dispatchId"] == "disp_test_001"
    assert dumped["executionSequence"] == 1
    assert dumped["tenantId"] == 10001
    assert "attemptNo" not in dumped
    assert "taskStatus" not in dumped
    assert "closeReason" not in dumped


def test_03b_required_fields_and_shared_bundle():
    task = _task_with_ia()
    outcome = build_call_outcome(task, result_bundle_id="rb_shared")
    dumped = outcome.model_dump(mode="json", exclude_none=True)
    assert dumped["producedAt"].endswith("+08:00")
    assert dumped["resultBundleId"] == "rb_shared"
    assert dumped["tenantId"] == 10001
    assert dumped["scenarioPayload"]["resendRequested"] is False


def test_03a_03b_share_default_bundle_id():
    task = _task_with_ia()
    event = build_call_event(task)
    outcome = build_call_outcome(task)
    assert event.resultBundleId == outcome.resultBundleId == "rb10001"


def test_03b_reschedule_clear_callback_time():
    task = _task_with_ia()
    task.result = "not_convenient"
    task.result_summary = "候选人开会，约今晚回电"
    task.telephony = {
        "business": {
            "result": "not_convenient",
            "promised_time": "2026年8月4日 18:00",
        },
    }
    outcome = build_call_outcome(task)
    rs = outcome.reschedule
    assert rs["reason"] == "RECIPIENT_CALLBACK_REQUEST"
    assert rs["triggerFamily"] == "BUSY_MEETING"
    assert rs["nextAction"]["type"] == "CALLBACK"
    assert rs["nextAction"]["channel"] == "VOICE"
    assert rs["nextAction"]["owner"] == "PLATFORM"
    assert rs["nextAction"]["dueTime"] == "2026-08-04T18:00:00+08:00"
    assert rs["nextAction"]["dueWindow"] is None
    assert rs["weakCommitment"] is False
    assert outcome.recipientPromisedTime == "2026-08-04T18:00:00+08:00"


def test_03b_reschedule_vague_keeps_due_window():
    task = _task_with_ia()
    task.result = "not_convenient"
    task.result_summary = "候选人不方便，下午再打"
    task.telephony = {
        "business": {"result": "not_convenient", "promised_time": "下午再打"},
    }
    outcome = build_call_outcome(task)
    rs = outcome.reschedule
    assert rs["reason"] == "RECIPIENT_CALLBACK_REQUEST"
    assert rs["triggerFamily"] == "VAGUE_LATER"
    assert rs["nextAction"]["type"] == "CALLBACK"
    assert rs["nextAction"]["dueTime"] is None
    assert rs["nextAction"]["dueWindow"] == "下午再打"
    assert rs["weakCommitment"] is True


def test_03a_dial_failure_reason_on_sip_failed():
    task = _task_with_ia()
    task.result = "call_failed"
    task.error = 'call failed: 500 (Q.850;cause=16;text="NORMAL_CLEARING")'
    task.result_summary = task.error
    task.telephony = {"sip": {"status": "500", "error": task.error}}
    event = build_call_event(task)
    assert event.callOutcome == "FAILED"
    assert event.attemptCounted is False
    assert event.dialFailureReason
    assert len(event.dialFailureReason) <= 500
    assert "NORMAL_CLEARING" not in event.dialFailureReason
    assert "Q.850" not in event.dialFailureReason
    assert "cause=16" not in event.dialFailureReason
    assert "call failed: 500" not in event.dialFailureReason
    assert "线路异常" in event.dialFailureReason or "abnormally" in event.dialFailureReason


def test_03b_no_answer_reports_retry():
    task = _task_with_ia()
    task.result = "no_answer"
    task.result_summary = "SIP 404 / no answer"
    task.error = "call failed: 404"
    event = build_call_event(task)
    outcome = build_call_outcome(task)
    assert event.callOutcome == "NO_ANSWER"
    assert event.dialFailureReason
    assert "404" not in event.dialFailureReason
    assert outcome.reschedule["reason"] == "NO_ANSWER_RETRY"
    assert outcome.reschedule["nextAction"]["type"] == "CALLBACK"
    assert outcome.reschedule["nextAction"]["owner"] == "PLATFORM"
    assert outcome.reschedule["nextAction"]["dueTime"] is None
    assert outcome.feedbackItems == []


def test_03b_system_fail_no_feedback_items():
    task = _task_with_ia()
    task.result = "call_failed"
    task.error = 'call failed: 500 (Q.850;cause=16;text="NORMAL_CLEARING")'
    event = build_call_event(task)
    outcome = build_call_outcome(task)
    assert event.dialFailureReason
    assert outcome.feedbackItems == []


def test_03b_followup_miss_feedback_only_dial_failure_reason():
    task = _task_with_ia(dispatchType="PROMISE_FOLLOW_UP")
    task.result = "no_answer"
    outcome = build_call_outcome(task)
    assert outcome.reschedule["nextAction"]["type"] == "NONE"
    assert isinstance(outcome.feedbackItems, list)
    assert len(outcome.feedbackItems) == 1
    assert set(outcome.feedbackItems[0].keys()) == {"dialFailureReason"}
    assert "未接听" in outcome.feedbackItems[0]["dialFailureReason"]


def test_05_meta_uses_product_at():
    meta = ArtifactUploadMeta(
        taskId=1,
        callId=2,
        artifactType="RECORDING",
        productAt="2026-08-03T17:02:00+08:00",
        tenantId=10001,
    )
    dumped = meta.model_dump(mode="json", exclude_none=True)
    assert dumped["tenantId"] == 10001
    assert dumped["productAt"] == "2026-08-03T17:02:00+08:00"
    assert "createdAt" not in dumped
    assert dumped["taskId"] == 1
