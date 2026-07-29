# -*- coding: utf-8 -*-
"""prod 通话结束后从转写抽取业务结果，供批次 / xlsx 回写。"""
from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any

# 结果码 → 交付可读说明（写入 result_summary / 可作 结果 列展示补充）
RESULT_LABELS_CN: dict[str, str] = {
    "no_email_feedback": "候选人拒绝提供邮箱，需交付跟进",
    "email_registered": "已登记候选人邮箱",
    "received_will_complete": "已收到短信并承诺完成时间",
    "promised_day_only": "仅承诺天级完成时间，需交付确认具体时刻",
    "not_convenient": "候选人不方便通话",
    "not_self": "非本人接听",
    "unclear": "结果不明确",
    "completed": "通话结束",
}

_EMAIL_RE = re.compile(
    r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}",
    re.IGNORECASE,
)

_EMAIL_ASK_HINTS = (
    "常用邮箱",
    "提供一个常用邮箱",
    "邮箱地址",
    "报一下具体的邮箱",
    "方便提供一个常用邮箱",
    "登记后通过这个邮箱",
)

_REFUSE_HINTS = (
    "不给",
    "不想给",
    "不方便给",
    "不提供",
    "不告诉",
    "没有邮箱",
    "没邮箱",
    "拒绝",
    "别问了",
    "不用了",
    "算了",
    "不发了",
    "不要发",
    "别发",
    "我不给",
    "不想提供",
    "不方便提供",
)

_FAREWELL_HINTS = (
    "祝您生活愉快",
    "祝你生活愉快",
)

_NOT_SELF_HINTS = (
    "不是本人",
    "打错了",
    "你打错",
    "不是我",
    "他不在",
    "她不在",
)

_NOT_CONVENIENT_HINTS = (
    "不方便",
    "在开车",
    "在开会",
    "现在不行",
    "晚点打",
    "回头再打",
)

# 改约回电口吻（相对「完成授权截止」承诺）
_CALLBACK_RESCHEDULE_HINTS = (
    "再联系",
    "再打",
    "回头打",
    "晚点打",
    "过会儿再",
    "分钟后",
    "半小时后",
    "一小时后",
    "1小时后",
)

_COMPLETION_PROMISE_HINTS = (
    "完成授权",
    "完成信息",
    "完善信息",
    "信息填写",
    "今天18点",
    "明天中午",
    "明天12",
    "明天下午2",
)

# 模型「内心回写」漏进转写时的结构化字段（优先于口播启发式）
_WRITEBACK_RESULT_RE = re.compile(
    r"(?:result\s*=\s*|内心回写[：:]\s*)([a-z_][a-z0-9_]*)",
    re.IGNORECASE,
)
_WRITEBACK_PROMISED_TIME_RE = re.compile(
    r"promised_time\s*=\s*([^；;\n）)\]]+)",
    re.IGNORECASE,
)
_WRITEBACK_PROMISED_DAY_RE = re.compile(
    r"promised_day\s*=\s*([^；;\n）)\]]+)",
    re.IGNORECASE,
)
_WRITEBACK_PRECISION_RE = re.compile(
    r"time_precision\s*=\s*(hour|day)",
    re.IGNORECASE,
)
_WRITEBACK_IN_MINUTES_RE = re.compile(
    r"promised_in_minutes\s*=\s*(\d{1,4})",
    re.IGNORECASE,
)

# 「5分钟后 / 过10分钟 / 半小时后 / 一小时后」
_REL_MINUTES_RE = re.compile(
    r"(?:过|再过)?\s*(\d{1,4})\s*分钟\s*(?:之?后|后再|以后|再(?:联系|打|来))?",
)
_REL_HALF_HOUR_RE = re.compile(r"半\s*小时\s*(?:之?后|后再)?")
_REL_ONE_HOUR_RE = re.compile(r"(?:一|1)\s*小时\s*(?:之?后|后再)?")

# 口播承诺时间：记录的是 / 约到 = 最终确认；尽量在 = 开场催办（易陈旧）
# 只截到「点前/点/当天内」，避免拖进「完成信息填写…」
_SPOKEN_PROMISED_RE = re.compile(
    r"(记录的是|约到|尽量在|按)"
    r"((?:今天|明天)?[^，。！？\n]{0,12}?(?:\d{1,2}\s*[:：]\s*\d{2}|点前|点|当天内))"
)

_YMD_HM_RE = re.compile(
    r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日"
    r"(?:\s*[^\d]{0,6})?(\d{1,2})\s*[:：]\s*(\d{2})"
)
_YMD_RE = re.compile(r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日")
_MD_HM_RE = re.compile(
    r"(\d{1,2})\s*月\s*(\d{1,2})\s*日\s*(\d{1,2})\s*[:：]\s*(\d{2})"
)
_MD_RE = re.compile(r"(\d{1,2})\s*月\s*(\d{1,2})\s*日")
_HM_COLON_RE = re.compile(r"(\d{1,2})\s*[:：]\s*(\d{2})")
_HM_DIAN_RE = re.compile(r"(中午|下午|上午)?\s*(\d{1,2})\s*点")


def _role_of(turn: dict[str, Any]) -> str:
    role = str(turn.get("role") or turn.get("speaker") or "").strip().lower()
    if role in {"user", "human", "candidate", "客户", "用户"}:
        return "user"
    if role in {"assistant", "ai", "bot", "agent", "助手"}:
        return "assistant"
    return role or "unknown"


def _text_of(turn: dict[str, Any]) -> str:
    return str(turn.get("text") or turn.get("content") or "").strip()


def _join_by_role(transcript: list[dict[str, Any]], role: str) -> str:
    bits = [_text_of(t) for t in transcript if _role_of(t) == role and _text_of(t)]
    return "\n".join(bits)


def _ai_asked_email(ai_text: str) -> bool:
    return any(h in ai_text for h in _EMAIL_ASK_HINTS)


def _user_refused_email(user_text: str) -> bool:
    return any(h in user_text for h in _REFUSE_HINTS)


def _find_emails(text: str) -> list[str]:
    return list(dict.fromkeys(_EMAIL_RE.findall(text or "")))


def _last_group(pattern: re.Pattern[str], text: str) -> str:
    matches = list(pattern.finditer(text or ""))
    if not matches:
        return ""
    return (matches[-1].group(1) or "").strip()


def _parse_writeback(ai_text: str) -> dict[str, str]:
    """Parse structured 内心回写 tags from assistant transcript (last wins)."""
    text = ai_text or ""
    result = _last_group(_WRITEBACK_RESULT_RE, text)
    promised_time = _last_group(_WRITEBACK_PROMISED_TIME_RE, text)
    promised_day = _last_group(_WRITEBACK_PROMISED_DAY_RE, text)
    precision = _last_group(_WRITEBACK_PRECISION_RE, text).lower()
    in_minutes = _last_group(_WRITEBACK_IN_MINUTES_RE, text)
    return {
        "result": result if result in RESULT_LABELS_CN else "",
        "promised_time": promised_time,
        "promised_day": promised_day,
        "time_precision": precision,
        "promised_in_minutes": in_minutes,
    }


def _extract_relative_minutes(text: str) -> int | None:
    """Return minutes offset from phrases like 5分钟后 / 半小时后."""
    raw = text or ""
    matches = list(_REL_MINUTES_RE.finditer(raw))
    if matches:
        try:
            return max(1, int(matches[-1].group(1)))
        except ValueError:
            pass
    if _REL_HALF_HOUR_RE.search(raw):
        return 30
    if _REL_ONE_HOUR_RE.search(raw):
        return 60
    return None


def absolute_from_relative_minutes(
    minutes: int,
    *,
    anchor: datetime | None = None,
) -> str:
    """Convert N minutes from now (Beijing) to display ``YYYY年M月D日 HH:MM``."""
    from config.prod_prompt import beijing_now, format_ymd_cn

    base = anchor or beijing_now()
    due = base + timedelta(minutes=max(1, int(minutes)))
    return f"{format_ymd_cn(due)} {due.hour:02d}:{due.minute:02d}"


def _looks_like_callback_reschedule(
    *,
    user_text: str,
    ai_text: str,
    relative_minutes: int | None,
    writeback_result: str = "",
) -> bool:
    """True when talk is 'call me later', not 'I'll finish auth by time T'."""
    if writeback_result == "not_convenient":
        return True
    if relative_minutes is not None and relative_minutes > 0:
        return True
    blob = f"{user_text}\n{ai_text}"
    if any(h in blob for h in _COMPLETION_PROMISE_HINTS):
        return False
    return any(h in blob for h in _CALLBACK_RESCHEDULE_HINTS)


def _extract_spoken_promised(ai_text: str) -> str:
    """Prefer final confirmation (记录的是/约到); never let opening 尽量在 win over later confirms."""
    matches = list(_SPOKEN_PROMISED_RE.finditer(ai_text or ""))
    if not matches:
        return ""
    confirms = [m for m in matches if m.group(1) in {"记录的是", "约到"}]
    chosen = confirms[-1] if confirms else matches[-1]
    return (chosen.group(2) or "").strip()


def _year_for_md(trigger: datetime, month: int, day: int) -> int:
    """Pick year for M月D日 relative to trigger (today/tomorrow / same calendar year)."""
    today = trigger.replace(hour=0, minute=0, second=0, microsecond=0)
    tomorrow = today + timedelta(days=1)
    if (today.month, today.day) == (month, day):
        return today.year
    if (tomorrow.month, tomorrow.day) == (month, day):
        return tomorrow.year
    # Dec 31 → Jan 1 next year etc.
    try:
        candidate = today.replace(month=month, day=day)
    except ValueError:
        return today.year
    if candidate < today - timedelta(days=180):
        return today.year + 1
    if candidate > today + timedelta(days=180):
        return today.year - 1
    return today.year


def _parse_clock(text: str) -> tuple[int, int] | None:
    m = _HM_COLON_RE.search(text or "")
    if m:
        return int(m.group(1)), int(m.group(2))
    m = _HM_DIAN_RE.search(text or "")
    if not m:
        return None
    period = m.group(1) or ""
    hour = int(m.group(2))
    if period == "下午" and 1 <= hour <= 11:
        hour += 12
    if period == "中午" and hour == 12:
        hour = 12
    return hour, 0


def normalize_promised_time_display(
    raw: str,
    *,
    trigger_beijing_at: str | datetime | None = None,
    relative_minutes: int | None = None,
) -> str:
    """Normalize promised time for xlsx: e.g. 2026年7月24日 12:00."""
    if relative_minutes is not None:
        try:
            return absolute_from_relative_minutes(int(relative_minutes))
        except (TypeError, ValueError):
            pass

    text = (raw or "").strip()
    if not text:
        return ""

    from config.prod_prompt import format_ymd_cn, parse_trigger_beijing

    # Relative phrases first (5分钟后 / 半小时后)
    rel = _extract_relative_minutes(text)
    if rel is not None:
        return absolute_from_relative_minutes(rel)

    trigger = parse_trigger_beijing(trigger_beijing_at)
    today = trigger.replace(hour=0, minute=0, second=0, microsecond=0)
    tomorrow = today + timedelta(days=1)

    m = _YMD_HM_RE.search(text)
    if m:
        y, mo, d, h, mi = (int(x) for x in m.groups())
        return f"{y}年{mo}月{d}日 {h:02d}:{mi:02d}"

    m = _MD_HM_RE.search(text)
    if m:
        mo, d, h, mi = (int(x) for x in m.groups())
        y = _year_for_md(trigger, mo, d)
        return f"{y}年{mo}月{d}日 {h:02d}:{mi:02d}"

    m = _YMD_RE.search(text)
    if m and not _parse_clock(text):
        y, mo, d = (int(x) for x in m.groups())
        return f"{y}年{mo}月{d}日"

    m = _MD_RE.search(text)
    if m and not _parse_clock(text):
        mo, d = int(m.group(1)), int(m.group(2))
        y = _year_for_md(trigger, mo, d)
        return f"{y}年{mo}月{d}日"

    day_base: datetime | None = None
    if "今天" in text:
        day_base = today
    elif "明天" in text:
        day_base = tomorrow

    if day_base is not None:
        if "当天内" in text and not _parse_clock(text):
            return format_ymd_cn(day_base)
        clock = _parse_clock(text)
        if clock:
            h, mi = clock
            return f"{format_ymd_cn(day_base)} {h:02d}:{mi:02d}"

    return text


def _pack_promise_outcome(
    *,
    code: str,
    promised: str,
    email: str,
    trigger_beijing_at: str | datetime | None,
) -> dict[str, str]:
    display = normalize_promised_time_display(
        promised, trigger_beijing_at=trigger_beijing_at
    )
    return {
        "result": code,
        "result_summary": RESULT_LABELS_CN[code] + (f"：{display}" if display else ""),
        "email": email,
        "promised_time": display,
    }


def extract_prod_outcome(
    transcript: list[dict[str, Any]] | None,
    *,
    fallback_result: str = "completed",
    fallback_summary: str = "",
    trigger_beijing_at: str | datetime | None = None,
) -> dict[str, str]:
    """Heuristic extract of prod business fields from call transcript.

    Returns keys aligned with ContactJob / xlsx:
      result, result_summary, email, promised_time

    promised_time is normalized to absolute Beijing calendar form, e.g.
    ``2026年7月24日 12:00``, using ``trigger_beijing_at`` as the today/tomorrow anchor.
    """
    turns = [t for t in (transcript or []) if isinstance(t, dict)]
    if not turns:
        return {
            "result": fallback_result or "completed",
            "result_summary": fallback_summary or RESULT_LABELS_CN.get("completed", "通话结束"),
            "email": "",
            "promised_time": "",
        }

    user_text = _join_by_role(turns, "user")
    ai_text = _join_by_role(turns, "assistant")
    all_text = "\n".join(_text_of(t) for t in turns if _text_of(t))

    emails_from_user = _find_emails(user_text)
    emails_confirmed = _find_emails(ai_text)
    # Prefer user-spoken address; else one AI restated during confirm.
    email = (emails_from_user[0] if emails_from_user else "") or (
        emails_confirmed[0] if emails_confirmed else ""
    )

    asked_email = _ai_asked_email(ai_text)
    refused = asked_email and _user_refused_email(user_text)
    farewell_after_ask = asked_email and any(h in ai_text for h in _FAREWELL_HINTS)

    # 拒绝提供邮箱：交付必须看见
    if (refused or farewell_after_ask) and not email:
        return {
            "result": "no_email_feedback",
            "result_summary": RESULT_LABELS_CN["no_email_feedback"],
            "email": "",
            "promised_time": "",
        }

    if email and asked_email:
        return {
            "result": "email_registered",
            "result_summary": f"{RESULT_LABELS_CN['email_registered']}：{email}",
            "email": email,
            "promised_time": "",
        }

    if any(h in user_text for h in _NOT_SELF_HINTS):
        return {
            "result": "not_self",
            "result_summary": RESULT_LABELS_CN["not_self"],
            "email": "",
            "promised_time": "",
        }

    if any(h in user_text for h in _NOT_CONVENIENT_HINTS) and "方便吗" in ai_text:
        # 仅在开场问方便后对方明确不方便时标记；避免误伤「邮箱不方便给」
        if not asked_email:
            writeback_nc = _parse_writeback(ai_text)
            rel_mins: int | None = None
            if writeback_nc.get("promised_in_minutes"):
                try:
                    rel_mins = int(writeback_nc["promised_in_minutes"])
                except ValueError:
                    rel_mins = None
            if rel_mins is None:
                rel_mins = _extract_relative_minutes(
                    (writeback_nc.get("promised_time") or "")
                    + "\n"
                    + ai_text
                    + "\n"
                    + user_text
                )
            promised_nc = (
                writeback_nc.get("promised_time")
                or _extract_spoken_promised(ai_text)
            )
            display_nc = normalize_promised_time_display(
                promised_nc,
                trigger_beijing_at=trigger_beijing_at,
                relative_minutes=rel_mins,
            )
            return {
                "result": "not_convenient",
                "result_summary": RESULT_LABELS_CN["not_convenient"]
                + (f"：{display_nc}" if display_nc else ""),
                "email": "",
                "promised_time": display_nc,
            }

    writeback = _parse_writeback(ai_text)
    rel_from_wb: int | None = None
    if writeback.get("promised_in_minutes"):
        try:
            rel_from_wb = int(writeback["promised_in_minutes"])
        except ValueError:
            rel_from_wb = None
    promised = (
        writeback.get("promised_time")
        or writeback.get("promised_day")
        or _extract_spoken_promised(ai_text)
    )
    if rel_from_wb is None:
        rel_from_wb = _extract_relative_minutes(
            (promised or "") + "\n" + ai_text + "\n" + user_text
        )
    wb_result = writeback.get("result") or ""
    precision = writeback.get("time_precision") or ""

    # 内心回写已给出明确结果码时优先采用（尤其是承诺时间类）
    if wb_result in {"received_will_complete", "promised_day_only"} and (
        promised or "收到" in all_text or "短信" in all_text
    ):
        if not promised and wb_result == "promised_day_only":
            day_only = True
        else:
            day_only = (
                wb_result == "promised_day_only"
                or precision == "day"
                or ("当天内" in promised and "点" not in promised)
            )
        code = "promised_day_only" if day_only else "received_will_complete"
        return _pack_promise_outcome(
            code=code,
            promised=promised,
            email=email,
            trigger_beijing_at=trigger_beijing_at,
        )

    if promised and ("收到" in all_text or "短信" in all_text):
        day_only = (
            precision == "day"
            or ("当天内" in promised and "点" not in promised)
        )
        code = "promised_day_only" if day_only else "received_will_complete"
        return _pack_promise_outcome(
            code=code,
            promised=promised,
            email=email,
            trigger_beijing_at=trigger_beijing_at,
        )

    if wb_result and wb_result in RESULT_LABELS_CN:
        display = normalize_promised_time_display(
            promised,
            trigger_beijing_at=trigger_beijing_at,
            relative_minutes=rel_from_wb if wb_result == "not_convenient" else None,
        )
        return {
            "result": wb_result,
            "result_summary": RESULT_LABELS_CN[wb_result]
            + (f"：{display}" if display else ""),
            "email": email,
            "promised_time": display,
        }

    # 改约回电：常被 fallback 成 completed，但仍带了 promised_time（如「1分钟后再打」）
    if _looks_like_callback_reschedule(
        user_text=user_text,
        ai_text=ai_text,
        relative_minutes=rel_from_wb,
        writeback_result=wb_result,
    ):
        display = normalize_promised_time_display(
            promised,
            trigger_beijing_at=trigger_beijing_at,
            relative_minutes=rel_from_wb,
        )
        return {
            "result": "not_convenient",
            "result_summary": RESULT_LABELS_CN["not_convenient"]
            + (f"：{display}" if display else ""),
            "email": email,
            "promised_time": display,
        }

    display = normalize_promised_time_display(
        promised,
        trigger_beijing_at=trigger_beijing_at,
        relative_minutes=rel_from_wb,
    )
    return {
        "result": fallback_result or "completed",
        "result_summary": fallback_summary
        or RESULT_LABELS_CN.get(fallback_result or "", "")
        or RESULT_LABELS_CN["completed"],
        "email": email,
        "promised_time": display,
    }
