# -*- coding: utf-8 -*-
"""tmp-prod 通话结束后从转写抽取业务结果，供批次 / xlsx 回写。"""
from __future__ import annotations

import re
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


def extract_tmp_prod_outcome(
    transcript: list[dict[str, Any]] | None,
    *,
    fallback_result: str = "completed",
    fallback_summary: str = "",
) -> dict[str, str]:
    """Heuristic extract of tmp-prod business fields from call transcript.

    Returns keys aligned with ContactJob / xlsx:
      result, result_summary, email, promised_time
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
            return {
                "result": "not_convenient",
                "result_summary": RESULT_LABELS_CN["not_convenient"],
                "email": "",
                "promised_time": "",
            }

    # 粗提承诺时间（模型复述里常见）
    promised = ""
    m = re.search(
        r"(?:记录的是|尽量在|约到|按)([^，。！？\n]{0,24}?(?:\d{1,2}\s*[:：]\s*\d{2}|点前|点)[^，。！？\n]{0,8})",
        ai_text,
    )
    if m:
        promised = m.group(1).strip()

    if promised and ("收到" in all_text or "短信" in all_text):
        day_only = "当天内" in promised and "点" not in promised
        code = "promised_day_only" if day_only else "received_will_complete"
        return {
            "result": code,
            "result_summary": RESULT_LABELS_CN[code] + (f"：{promised}" if promised else ""),
            "email": email,
            "promised_time": promised,
        }

    return {
        "result": fallback_result or "completed",
        "result_summary": fallback_summary
        or RESULT_LABELS_CN.get(fallback_result or "", "")
        or RESULT_LABELS_CN["completed"],
        "email": email,
        "promised_time": promised,
    }
