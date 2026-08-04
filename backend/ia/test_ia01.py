# -*- coding: utf-8 -*-
"""IA-01 unit tests (no SIP)."""
from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

# Force dry-run before importing app/outbound dial.
os.environ["OUTBOUND_DRY_RUN"] = "true"
os.environ.pop("IA_ALGO_API_KEY", None)
os.environ.pop("IA_PLATFORM_BASE_URL", None)


SAMPLE = {
    "dispatchId": "disp_test_001",
    "taskRevision": 1,
    "dispatchType": "INITIAL",
    "taskId": 1948372615340032001,
    "externalOrderId": "order123",
    "scenario": "AUTHORIZATION_REMINDER",
    "scriptId": "authReminder",
    "scriptVersion": "2.3",
    "recipient": {"name": "张三", "mobile": "13800138000", "email": None},
    "identity": {
        "mode": "BRAND_SELF",
        "customerBrandName": None,
        "ourBrandName": "芝麻背调",
        "clientBuName": "比赛日科技",
    },
    "slots": {
        "contact": {
            "callbackNumber": "4008000000",
            "workingHours": "09:00-18:00",
            "workingHoursTimezone": "Asia/Shanghai",
        },
        "channels": {
            "emailEnabled": False,
            "fallbackSmsEnabled": True,
            "initialSmsSentAt": "2026-07-22T10:00:00+08:00",
            "fallbackSmsSentAt": "2026-07-22T12:00:00+08:00",
            "smsTextPrefix": None,
            "emailSubject": None,
            "fallbackSmsContent": "【示例】请点击链接完成授权……",
            "publicAccountName": "示例公众号",
            "publicAccountOwner": "PLATFORM",
            "resendChannel": "FALLBACK_SMS",
        },
        "order": {
            "orderType": "ONLINE",
            "authorizationStatus": "NOT_COMPLETED",
            "statusUpdatedAt": "2026-07-22T12:30:00+08:00",
            "deadline": "2026-07-23T18:00:00+08:00",
            "authorizationMode": "ELECTRONIC",
        },
        "workOrderNo": "WO-123",
    },
    "context": {
        "historyLinked": False,
        "previousTaskId": None,
        "lastSpokenSummary": None,
        "remainingItems": None,
        "recipientPromisedTime": None,
        "lastTriggerFamily": None,
        "clientNoteFiltered": "候选人已与 HR 确认，优先说明截止时间",
    },
    "callWindow": "09:00-21:00",
    "timezone": "Asia/Shanghai",
    "requestedAt": None,
    "deadline": None,
    "retryPolicy": {"maxAttempts": 2, "retryIntervalMinutes": 30},
}


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("OUTBOUND_DRY_RUN", "true")
    from fastapi import FastAPI

    from backend.ia import store as ia_store
    from backend.ia.routes import router as ia_router

    ia_store._STORE = None  # noqa: SLF001
    app = FastAPI()
    app.include_router(ia_router)
    return TestClient(app)


def test_task_dispatch_accept_and_idempotent(client: TestClient):
    body = dict(SAMPLE)
    body["dispatchId"] = "disp_idem_001"
    r1 = client.post("/va-algo/v1/task-dispatch", json=body)
    assert r1.status_code == 200, r1.text
    data1 = r1.json()
    assert data1["accepted"] is True
    assert data1["duplicate"] is False
    assert "plannedCallAt" not in data1
    assert data1["taskRevision"] == 1
    assert set(data1.keys()) == {"accepted", "duplicate", "taskRevision"}

    r2 = client.post("/va-algo/v1/task-dispatch", json=body)
    assert r2.status_code == 200, r2.text
    data2 = r2.json()
    assert data2["duplicate"] is True
    assert data2["taskRevision"] == data1["taskRevision"]
    assert "plannedCallAt" not in data2


def test_task_dispatch_rejects_bad_script(client: TestClient):
    body = dict(SAMPLE)
    body["dispatchId"] = "disp_bad_script"
    body["scriptId"] = "otherScript"
    r = client.post("/va-algo/v1/task-dispatch", json=body)
    assert r.status_code == 400
    assert r.json()["errorCode"] == "INVALID_DISPATCH"


def test_task_dispatch_accepts_tenant_id_and_subject_key(client: TestClient):
    body = dict(SAMPLE)
    body["dispatchId"] = "disp_tenant_001"
    body["tenantId"] = 10001
    body["subjectKey"] = "optional-keyword"  # 催授通常不传；有则透传
    r = client.post("/va-algo/v1/task-dispatch", json=body)
    assert r.status_code == 200, r.text
    assert r.json()["accepted"] is True

    from backend.ia.mapping import dispatch_to_outbound_payload
    from backend.ia.models import TaskDispatchRequest

    req = TaskDispatchRequest.model_validate(body)
    payload = dispatch_to_outbound_payload(req)
    assert payload["ia"]["tenantId"] == 10001
    assert payload["ia"]["subjectKey"] == "optional-keyword"


def test_task_dispatch_omits_tenant_and_subject_key_ok(client: TestClient):
    """Smoke: tenantId / subjectKey still optional."""
    body = dict(SAMPLE)
    body["dispatchId"] = "disp_no_tenant"
    assert "tenantId" not in body
    assert "subjectKey" not in body
    r = client.post("/va-algo/v1/task-dispatch", json=body)
    assert r.status_code == 200, r.text
