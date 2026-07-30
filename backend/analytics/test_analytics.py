# -*- coding: utf-8 -*-
"""Analytics funnel + rollup unit tests (no SIP)."""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.analytics.funnel import infer_hangup_stage
from backend.analytics.meter import CallUsageSession, record_call_analytics, record_component_usage
from backend.analytics.models import CallAnalyticsRecord
from backend.analytics.providers.doubao import DoubaoUsageAdapter
from backend.analytics.providers.openai_compat import extract_openai_usage
from backend.analytics.rollup import rebuild_daily
from backend.analytics.store import reset_analytics_store_for_tests


@pytest.fixture()
def analytics_tmp(tmp_path: Path):
    store = reset_analytics_store_for_tests(tmp_path / "analytics")
    return store


def test_funnel_not_connected():
    stage, conf = infer_hangup_stage(connected=False, scenario="prod_auth_demo")
    assert stage == "not_connected"
    assert conf == "not_connected"


def test_funnel_prod_email_result():
    stage, conf = infer_hangup_stage(
        scenario="prod_auth_demo",
        connected=True,
        business_result="email_registered",
        transcript=[{"role": "user", "text": "我的邮箱是 a@b.com"}],
    )
    assert stage == "email_collect_done"
    assert conf == "result"


def test_funnel_prod_heuristic_spam():
    stage, conf = infer_hangup_stage(
        scenario="prod_auth_demo",
        connected=True,
        transcript=[
            {"role": "assistant", "text": "请问您收到背调授权短信了吗？"},
            {"role": "user", "text": "没收到"},
            {"role": "assistant", "text": "您方便看一下手机的骚扰拦截或垃圾短信箱吗？"},
        ],
    )
    assert stage == "spam_check"
    assert conf == "heuristic"


def test_local_usage_and_rollup(analytics_tmp):
    session = CallUsageSession("c1", scenario="prod_auth_demo")
    session.add_input_pcm16(b"\x00\x00" * 16000)  # 1s @ 16k
    session.add_output_pcm16(b"\x00\x00" * 24000)  # 1s @ 24k
    session.on_provider_event(
        {
            "type": "response.done",
            "usage": {"input_tokens": 10, "output_tokens": 20, "total_tokens": 30},
        }
    )
    rec = session.build_record(
        connected=True,
        hangup_reason="remote_bye",
        business_result="email_registered",
        transcript=[
            {"role": "assistant", "text": "开场"},
            {"role": "user", "text": "邮箱 a@b.com"},
        ],
        duration_sec=42.5,
    )
    assert rec.usage["local"]["audio_in_sec"] == pytest.approx(1.0, abs=0.01)
    assert rec.usage["local"]["audio_out_sec"] == pytest.approx(1.0, abs=0.01)
    assert rec.usage["provider_session"]["total_tokens"] == 30
    assert rec.hangup_stage == "email_collect_done"
    record_call_analytics(rec)

    record_component_usage(
        "c1",
        "followup",
        {"input_tokens": 5, "output_tokens": 7, "total_tokens": 12},
        provider="kimi",
    )
    day = rec.ended_at[:10] if rec.ended_at else None
    # Day bucket is Asia/Shanghai — rebuild from store list.
    from backend.analytics.store import _day_key

    day = _day_key(rec.ended_at)
    daily = rebuild_daily(day, store=analytics_tmp)
    assert daily["calls"] == 1
    assert daily["connected_calls"] == 1
    assert daily["avg_duration_sec"] == pytest.approx(42.5)
    assert daily["hangup_stage"].get("email_collect_done") == 1
    assert daily["usage_local_totals"]["total_tokens"] == 12  # followup component
    assert daily["usage_provider_session_totals"]["total_tokens"] == 30


def test_doubao_adapter_session_and_skip_pull():
    adapter = DoubaoUsageAdapter()
    deltas = adapter.on_session_event(
        {"type": "response.done", "usage": {"prompt_tokens": 3, "completion_tokens": 4}}
    )
    assert len(deltas) == 1
    assert deltas[0].metrics["input_tokens"] == 3
    assert deltas[0].metrics["output_tokens"] == 4


def test_doubao_day_pull_skipped_without_creds():
    import asyncio

    adapter = DoubaoUsageAdapter()
    result = asyncio.run(adapter.pull_day_usage("2026-07-30"))
    assert result.status == "skipped"


def test_openai_usage_extract():
    metrics = extract_openai_usage(
        {"usage": {"prompt_tokens": 11, "completion_tokens": 9, "total_tokens": 20}}
    )
    assert metrics == {
        "input_tokens": 11.0,
        "output_tokens": 9.0,
        "total_tokens": 20.0,
    }


def test_retention_purge(analytics_tmp, monkeypatch):
    monkeypatch.setenv("ANALYTICS_RETENTION_DAYS", "14")
    old = analytics_tmp.calls_dir / "2020-01-01.jsonl"
    analytics_tmp._ensure_dirs()
    old.write_text('{"call_id":"old"}\n', encoding="utf-8")
    old_daily = analytics_tmp.daily_dir / "2020-01-01.json"
    old_daily.write_text("{}\n", encoding="utf-8")
    keep_day = analytics_tmp.cutoff_day()
    keep = analytics_tmp.calls_dir / f"{keep_day}.jsonl"
    keep.write_text('{"call_id":"keep"}\n', encoding="utf-8")
    result = analytics_tmp.purge_expired()
    assert "2020-01-01" in result["removed_calls"]
    assert not old.exists()
    assert not old_daily.exists()
    assert keep.exists()


def test_range_summary_api(analytics_tmp, monkeypatch):
    monkeypatch.delenv("ANALYTICS_API_KEY", raising=False)
    monkeypatch.delenv("OUTBOUND_API_KEY", raising=False)
    from fastapi import FastAPI

    from backend.analytics.meter import CallUsageSession, record_call_analytics
    from backend.analytics.routes import router as analytics_router

    session = CallUsageSession("sum1", scenario="prod_auth_demo")
    rec = session.build_record(
        connected=True,
        hangup_reason="remote_bye",
        business_result="email_registered",
        transcript=[{"role": "user", "text": "ok"}],
        duration_sec=12.0,
    )
    record_call_analytics(rec)

    app = FastAPI()
    app.include_router(analytics_router)
    client = TestClient(app)
    r = client.get("/analytics/v1/summary?days=14")
    assert r.status_code == 200
    body = r.json()["summary"]
    assert body["retention_days"] == 14
    assert body["calls"] >= 1
    assert "series" in body
    assert len(body["series"]) == 14


def test_analytics_api(analytics_tmp, monkeypatch):
    monkeypatch.delenv("ANALYTICS_API_KEY", raising=False)
    monkeypatch.delenv("OUTBOUND_API_KEY", raising=False)
    from fastapi import FastAPI

    from backend.analytics.routes import router as analytics_router
    from backend.analytics.store import _day_key

    rec = CallAnalyticsRecord(
        call_id="api_call_1",
        scenario="prod_auth_demo",
        connected=True,
        duration_sec=10.0,
        hangup_stage="opening",
        hangup_reason="remote_bye",
        ended_at="2026-07-30T04:00:00+00:00",
        started_at="2026-07-30T03:59:50+00:00",
        usage={"local": {"audio_in_sec": 1.0}},
    )
    analytics_tmp.put(rec)

    app = FastAPI()
    app.include_router(analytics_router)
    client = TestClient(app)
    r = client.get("/analytics/v1/calls/api_call_1")
    assert r.status_code == 200
    assert r.json()["record"]["call_id"] == "api_call_1"

    day = _day_key(rec.ended_at)
    r2 = client.get(f"/analytics/v1/daily?day={day}&rebuild=true")
    assert r2.status_code == 200
    assert r2.json()["daily"]["calls"] >= 1