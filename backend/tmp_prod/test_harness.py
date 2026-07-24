# -*- coding: utf-8 -*-
"""Unit tests for tmp-prod harness policy / due-at parsing / prompt inject."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from backend.tmp_prod.harness.models import (
    ACTION_CALLBACK_RESCHEDULE,
    ACTION_RETRY_NO_ANSWER,
    CALL_KIND_RESCHEDULE,
)
from backend.tmp_prod.harness.policy import plan_next_action, promised_display_to_due_utc
from backend.tmp_prod.outcome import extract_tmp_prod_outcome
from config.tmp_prod_prompt import (
    build_tmp_prod_opening,
    build_tmp_prod_system_role,
    tmp_prod_slots_from_payload,
)

BJ = ZoneInfo("Asia/Shanghai")


def test_promised_display_to_due_utc_hour_level():
    due = promised_display_to_due_utc("2026年7月24日 15:30")
    assert due is not None
    local = due.astimezone(BJ)
    assert local.year == 2026 and local.month == 7 and local.day == 24
    assert local.hour == 15 and local.minute == 30


def test_promised_display_day_only_no_schedule():
    assert promised_display_to_due_utc("2026年7月24日") is None


def test_plan_no_answer_retry():
    now = datetime(2026, 7, 24, 4, 0, tzinfo=timezone.utc)
    planned = plan_next_action(
        outcome={"result": "no_answer"},
        attempt=1,
        max_attempts=2,
        is_no_answer=True,
        now=now,
        retry_delay_sec=1800,
    )
    assert planned is not None
    assert planned.type == ACTION_RETRY_NO_ANSWER
    assert planned.contact_status == "waiting_retry"
    assert planned.due_at == now + timedelta(seconds=1800)


def test_plan_no_answer_exhausted():
    assert (
        plan_next_action(
            outcome={"result": "no_answer"},
            attempt=2,
            max_attempts=2,
            is_no_answer=True,
        )
        is None
    )


def test_plan_reschedule_callback():
    now = datetime(2026, 7, 24, 4, 0, tzinfo=timezone.utc)  # 12:00 BJ
    planned = plan_next_action(
        outcome={
            "result": "not_convenient",
            "result_summary": "候选人不方便通话：2026年7月24日 16:00",
            "promised_time": "2026年7月24日 16:00",
            "email": "",
        },
        attempt=1,
        is_no_answer=False,
        source_call_id="abc",
        now=now,
    )
    assert planned is not None
    assert planned.type == ACTION_CALLBACK_RESCHEDULE
    assert planned.contact_status == "waiting_callback"
    assert planned.callback_context["call_kind"] == CALL_KIND_RESCHEDULE
    assert planned.callback_context["prior_call_id"] == "abc"
    local = planned.due_at.astimezone(BJ)
    assert local.hour == 16 and local.minute == 0


def test_plan_reschedule_without_hour_skipped():
    assert (
        plan_next_action(
            outcome={
                "result": "not_convenient",
                "promised_time": "",
            },
            attempt=1,
            is_no_answer=False,
        )
        is None
    )


def test_plan_completed_promise_not_scheduled():
    """MVP: received_will_complete does not auto-callback."""
    assert (
        plan_next_action(
            outcome={
                "result": "received_will_complete",
                "promised_time": "2026年7月24日 18:00",
            },
            attempt=1,
            is_no_answer=False,
        )
        is None
    )


def test_outcome_not_convenient_keeps_promised_time():
    out = extract_tmp_prod_outcome(
        [
            {"role": "assistant", "text": "大约占用您两分钟，现在方便吗？"},
            {"role": "user", "text": "现在不方便，在开会"},
            {
                "role": "assistant",
                "text": (
                    "好的，那我今天下午4点再联系您。"
                    "（内心回写：result=not_convenient；"
                    "promised_time=2026年7月24日 16:00；time_precision=hour）"
                ),
            },
        ],
        trigger_beijing_at="2026-07-24T10:00:00+08:00",
    )
    assert out["result"] == "not_convenient"
    assert out["promised_time"] == "2026年7月24日 16:00"


def test_reschedule_prompt_inject():
    slots = tmp_prod_slots_from_payload(
        {
            "candidate_name": "张三",
            "employer": "比赛日科技",
            "client_name": "比赛日科技",
            "our_brand_name": "芝麻背调",
            "identity_mode": "BRAND_SELF",
            "call_kind": CALL_KIND_RESCHEDULE,
            "callback_context": {
                "call_kind": CALL_KIND_RESCHEDULE,
                "prior_result": "not_convenient",
                "prior_summary": "开会",
                "promised_time": "2026年7月24日 16:00",
                "email": "",
                "progress": {"sms_confirmed": False, "email_registered": False},
                "prior_call_id": "c1",
            },
            "trigger_beijing_at": "2026-07-24T16:00:00+08:00",
        }
    )
    opening = build_tmp_prod_opening(**slots)
    role = build_tmp_prod_system_role(**slots)
    assert "按您上次说的时间" in opening
    assert "2026年7月24日 16:00" in opening
    assert "跨通话上下文" in role
    assert "改约回电" in role
    assert "完整催办开场" not in opening
