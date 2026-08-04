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
        "attemptNo": 2,
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


def test_03a_missing_tenant_id_raises():
    task = _task_with_ia()
    # override after defaults: pop tenantId
    del task.payload["ia"]["tenantId"]
    with pytest.raises(ValueError, match="tenantId"):
        build_call_event(task)


def test_03a_required_fields_and_no_deprecated_status():
    task = _task_with_ia()
    event = build_call_event(task, result_bundle_id="rb_shared")
    dumped = event.model_dump(mode="json", exclude_none=True)
    assert dumped["occurredAt"].endswith("+08:00")
    assert dumped["resultBundleId"] == "rb_shared"
    assert dumped["attemptNo"] == 2
    assert dumped["dispatchId"] == "disp_test_001"
    assert dumped["executionSequence"] == 1
    assert dumped["tenantId"] == 10001
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


def test_03b_converts_chinese_promised_time_to_iso():
    task = _task_with_ia()
    task.telephony = {
        "business": {"promised_time": "2026年8月4日 18:00"},
    }
    outcome = build_call_outcome(task)
    assert outcome.recipientPromisedTime == "2026-08-04T18:00:00+08:00"
    assert outcome.reschedule["nextAction"]["dueTime"] == "2026-08-04T18:00:00+08:00"
    assert outcome.reschedule["reason"] == "RECIPIENT_CALLBACK_REQUEST"


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
