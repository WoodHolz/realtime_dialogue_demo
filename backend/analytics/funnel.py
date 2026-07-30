# -*- coding: utf-8 -*-
"""Hangup-stage funnel inference (scenario-pluggable)."""
from __future__ import annotations

from typing import Any


def hangup_party_from_reason(hangup_reason: str) -> str:
    reason = (hangup_reason or "").strip().lower()
    if not reason:
        return "unknown"
    if reason in {"remote_bye", "remote", "peer_bye", "bye_remote"}:
        return "remote"
    if reason in {
        "user_idle_timeout",
        "max_duration",
        "bridge_done",
        "local_bye",
        "agent_error",
    }:
        return "local"
    if "error" in reason or reason in {"failed", "reject"}:
        return "system"
    if "bye" in reason and "remote" in reason:
        return "remote"
    if "bye" in reason:
        return "local"
    return "unknown"


def _join_transcript(transcript: list[dict[str, Any]] | None) -> tuple[str, str, int, int]:
    user_parts: list[str] = []
    asst_parts: list[str] = []
    turns_user = 0
    turns_asst = 0
    for row in transcript or []:
        if not isinstance(row, dict):
            continue
        role = str(row.get("role") or "").lower()
        text = str(row.get("text") or "").strip()
        if role in {"user", "human"}:
            turns_user += 1
            if text:
                user_parts.append(text)
        elif role in {"assistant", "ai", "bot"}:
            turns_asst += 1
            if text:
                asst_parts.append(text)
    return "\n".join(user_parts), "\n".join(asst_parts), turns_user, turns_asst


def _infer_prod_stage(
    *,
    business_result: str,
    user_text: str,
    asst_text: str,
    turns_user: int,
    turns_asst: int,
) -> tuple[str, str]:
    result = (business_result or "").strip()
    result_stage = {
        "email_registered": "email_collect_done",
        "no_email_feedback": "email_refused",
        "received_will_complete": "received_deadline",
        "promised_day_only": "received_deadline",
        "not_convenient": "convenience",
        "not_self": "opening",
        "unclear": "delivery_confirm",
        "completed": "closing",
        "no_answer": "not_connected",
        "busy": "not_connected",
        "call_failed": "not_connected",
    }
    if result in result_stage:
        return result_stage[result], "result"

    # Heuristic depth along prod script path (deepest match wins).
    stage = "opening"
    confidence = "heuristic"

    if turns_asst == 0 and turns_user == 0:
        return "opening", confidence

    if turns_user == 0:
        return "opening", confidence

    # After at least one user turn, treat as past opening / convenience gate.
    stage = "convenience"

    delivery_hints = ("授权短信", "背调授权", "收到", "短信")
    if any(h in asst_text for h in delivery_hints) or any(h in user_text for h in ("收到", "没收到", "找不到")):
        stage = "delivery_confirm"

    if any(h in asst_text for h in ("尽快打开短信", "完善个人信息", "今天", "明天", "完成授权")):
        if any(h in user_text for h in ("收到", "有收到", "看到了", "找到了")) or "催办" in asst_text:
            stage = "received_deadline"

    if any(h in asst_text for h in ("骚扰拦截", "垃圾短信")):
        stage = "spam_check"
    if any(h in asst_text for h in ("不带链接", "另外收到一条")):
        stage = "alt_sms"
    if any(h in asst_text for h in ("常用邮箱", "邮箱地址", "登记后通过这个邮箱")):
        stage = "email_collect"
    if any(h in asst_text for h in ("已经为您登记", "祝您生活愉快", "再见")):
        stage = "closing"

    return stage, confidence


def _infer_generic_stage(
    *,
    business_result: str,
    turns_user: int,
    turns_asst: int,
) -> tuple[str, str]:
    if business_result:
        return "completed", "result"
    if turns_user == 0 and turns_asst <= 1:
        return "opening", "heuristic"
    if turns_user == 0:
        return "opening", "heuristic"
    if turns_asst <= 1:
        return "early_dialogue", "heuristic"
    return "mid_dialogue", "heuristic"


def infer_hangup_stage(
    *,
    scenario: str = "",
    connected: bool = True,
    hangup_reason: str = "",
    business_result: str = "",
    transcript: list[dict[str, Any]] | None = None,
) -> tuple[str, str]:
    """
    Return (hangup_stage, confidence).

    confidence: not_connected | result | heuristic | reason_only
    """
    if not connected:
        return "not_connected", "not_connected"

    reason = (hangup_reason or "").strip().lower()
    # Keep dialogue progress as stage; reason stays on the record separately.
    user_text, asst_text, turns_user, turns_asst = _join_transcript(transcript)

    scenario_l = (scenario or "").strip().lower()
    is_prod = scenario_l in {
        "prod_auth_demo",
        "prod",
        "tmp_prod",
        "tmp-prod",
        "authorization_reminder",
        "urge_auth",
    } or "prod" in scenario_l or "auth" in scenario_l

    if is_prod:
        stage, confidence = _infer_prod_stage(
            business_result=business_result,
            user_text=user_text,
            asst_text=asst_text,
            turns_user=turns_user,
            turns_asst=turns_asst,
        )
    else:
        stage, confidence = _infer_generic_stage(
            business_result=business_result,
            turns_user=turns_user,
            turns_asst=turns_asst,
        )

    if confidence == "heuristic" and not user_text and not asst_text and reason:
        return stage, "reason_only"
    return stage, confidence
