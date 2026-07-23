# -*- coding: utf-8 -*-
"""Unit tests for tmp-prod outcome extraction."""
from __future__ import annotations

from backend.tmp_prod.outcome import (
    extract_tmp_prod_outcome,
    normalize_promised_time_display,
)

# Call placed on 2026-07-23 Beijing → 明天 = 2026-07-24
_TRIGGER = "2026-07-23T15:00:00+08:00"


def _turns(*pairs: tuple[str, str]) -> list[dict[str, str]]:
    return [{"role": role, "text": text} for role, text in pairs]


def test_normalize_relative_tomorrow_noon():
    assert (
        normalize_promised_time_display(
            "明天中午12点前", trigger_beijing_at=_TRIGGER
        )
        == "2026年7月24日 12:00"
    )


def test_normalize_md_writeback_adds_year():
    assert (
        normalize_promised_time_display(
            "7月24日 12:00", trigger_beijing_at=_TRIGGER
        )
        == "2026年7月24日 12:00"
    )


def test_reschedule_to_tomorrow_noon_uses_absolute_ymd():
    """Opening 今天18点前 must not beat final 明天12点; xlsx gets full YMD."""
    transcript = _turns(
        (
            "assistant",
            "好的，辛苦您尽快打开短信，按照页面提示完善个人信息并完成授权。"
            "为了不影响后续背调进度，请您尽量在今天18点前完成。您看可以吗？",
        ),
        ("user", "明天中午吧"),
        (
            "assistant",
            "好的，理解。那我先帮您约到明天中午12点前，您看可以吗？",
        ),
        ("user", "我看明天中午 12 点前。那可以可以。"),
        (
            "assistant",
            "好的，我记录的是明天中午12点前完成信息填写和授权。辛苦您了，谢谢。"
            "那先这样，您先挂机就可以，再见。",
        ),
        ("user", "那好，再见。再见。"),
        (
            "assistant",
            "（内心回写：received_will_complete；promised_time=2026年7月24日 12:00；time_precision=hour）",
        ),
    )
    out = extract_tmp_prod_outcome(transcript, trigger_beijing_at=_TRIGGER)
    assert out["result"] == "received_will_complete"
    assert out["promised_time"] == "2026年7月24日 12:00"
    assert out["result_summary"] == "已收到短信并承诺完成时间：2026年7月24日 12:00"
    assert "明天" not in out["promised_time"]
    assert "今天18点" not in out["promised_time"]


def test_spoken_confirmation_normalizes_without_writeback():
    transcript = _turns(
        (
            "assistant",
            "请您尽量在今天18点前完成。您看可以吗？",
        ),
        ("user", "明天中午可以"),
        (
            "assistant",
            "那我先帮您约到明天中午12点前，您看可以吗？",
        ),
        ("user", "可以"),
        (
            "assistant",
            "好的，我记录的是明天中午12点前完成信息填写和授权。短信您收到了吧？",
        ),
        ("user", "收到了"),
    )
    out = extract_tmp_prod_outcome(transcript, trigger_beijing_at=_TRIGGER)
    assert out["result"] == "received_will_complete"
    assert out["promised_time"] == "2026年7月24日 12:00"
    assert "完成信息填写" not in out["promised_time"]


def test_today_18_still_works_when_accepted():
    transcript = _turns(
        (
            "assistant",
            "请您尽量在今天18点前完成。您看可以吗？",
        ),
        ("user", "可以，今天搞完"),
        (
            "assistant",
            "好的，我记录的是今天18点前完成信息填写和授权。"
            "（内心回写：received_will_complete；promised_time=7月23日 18:00；time_precision=hour）",
        ),
    )
    out = extract_tmp_prod_outcome(transcript, trigger_beijing_at=_TRIGGER)
    assert out["result"] == "received_will_complete"
    assert out["promised_time"] == "2026年7月23日 18:00"
