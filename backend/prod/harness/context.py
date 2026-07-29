# -*- coding: utf-8 -*-
"""Build compact callback_context for cross-call memory injection."""
from __future__ import annotations

from typing import Any

from backend.prod.harness.models import CALL_KIND_RESCHEDULE


def build_callback_context(
    *,
    outcome: dict[str, Any],
    source_call_id: str = "",
    contact_email: str = "",
) -> dict[str, Any]:
    """Structured memory for the next outbound; keep short — no full transcript."""
    result = str(outcome.get("result") or "").strip()
    summary = str(outcome.get("result_summary") or "").strip()
    promised = str(outcome.get("promised_time") or "").strip()
    email = str(outcome.get("email") or contact_email or "").strip()

    sms_confirmed = result in {
        "received_will_complete",
        "promised_day_only",
        "email_registered",
    }
    # not_convenient usually hung before delivery confirm
    if result == "not_convenient":
        sms_confirmed = False

    return {
        "call_kind": CALL_KIND_RESCHEDULE,
        "prior_result": result,
        "prior_summary": summary,
        "promised_time": promised,
        "email": email,
        "progress": {
            "sms_confirmed": sms_confirmed,
            "email_registered": bool(email) or result == "email_registered",
        },
        "prior_call_id": (source_call_id or "").strip(),
    }


def summarize_context_for_prompt(ctx: dict[str, Any] | None) -> str:
    """Human-readable block injected into system_role."""
    c = dict(ctx or {})
    if not c:
        return ""
    progress = c.get("progress") if isinstance(c.get("progress"), dict) else {}
    lines = [
        "【跨通话上下文（系统注入，勿向对方朗读本块）】",
        f"- 本通类型：改约回电（call_kind={c.get('call_kind') or CALL_KIND_RESCHEDULE}）",
        f"- 上次结果：{c.get('prior_result') or '（无）'}",
        f"- 上次摘要：{c.get('prior_summary') or '（无）'}",
        f"- 约定联系时间：{c.get('promised_time') or '（无）'}",
    ]
    if c.get("email"):
        lines.append(f"- 已登记邮箱：{c.get('email')}（勿再索要，除非对方主动更正）")
    if progress.get("sms_confirmed"):
        lines.append("- 进度：上次已确认收到授权短信")
    else:
        lines.append("- 进度：上次未完成送达确认，本通仍需确认是否收到授权短信")
    if c.get("prior_call_id"):
        lines.append(f"- 上次 call_id：{c.get('prior_call_id')}")
    lines.append(
        "- 开场用改约短开场；对方方便后按进度继续主流程，不要假装记得整段对话原文。"
    )
    return "\n".join(lines)
