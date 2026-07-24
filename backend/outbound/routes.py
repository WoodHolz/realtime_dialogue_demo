# -*- coding: utf-8 -*-
"""Partner-facing REST API for AI outbound calls (催授权场景)."""
from __future__ import annotations

import logging
import os
from typing import Any

from fastapi import APIRouter, Header, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from backend.outbound.models import RESULT_ALREADY_DONE, STATUS_COMPLETED
from backend.doubao_voices import list_voice_catalog, resolve_voice
from backend.outbound.service import (
    SUPPORTED_SCENARIOS,
    build_scenario_prompt,
    complete_call,
    create_outbound_call,
    verify_partner_api_key,
)
from backend.outbound.dial import load_dial_settings, normalize_phone
from backend.outbound.store import get_call_store

LOG = logging.getLogger(__name__)

router = APIRouter(prefix="/outbound/v1", tags=["partner-outbound"])


class CreateCallRequest(BaseModel):
    phone: str = Field(..., description="被叫手机号（系统会自动加 a1 前缀）")
    scenario: str = Field(default="urge_auth", description="目前仅支持 urge_auth（催授权）")
    external_id: str = Field(default="", description="合作方侧业务单号，原样回传")
    callback_url: str = Field(default="", description="通话结束后 POST 结果到此地址")
    candidate_name: str = Field(default="", description="候选人称呼，如 张先生")
    employer: str = Field(default="", description="用人单位（tmp_prod 场景）；空则回退 client_name")
    gender: str = Field(default="", description="性别（tmp_prod 场景可选）")
    client_name: str = Field(
        default="",
        description="白标=对外自称品牌（客户B）；BRAND_SELF=委托方；tmp_prod 亦可作用人单位回退",
    )
    identity_mode: str = Field(
        default="CLIENT_IDENTITY",
        description=(
            "白标 CLIENT_IDENTITY / CHANNEL_BRAND（同族，以客户B名义）；"
            "直客 BRAND_SELF（我方品牌+受托）"
        ),
    )
    our_brand_name: str = Field(default="芝麻背调", description="仅 BRAND_SELF 我方品牌")
    channel_brand_name: str = Field(
        default="",
        description="兼容旧字段：白标时非空则覆盖 client_name 作为对外自称品牌（客户B）",
    )
    invite_channel: str = Field(default="短信", description="已发送渠道文案，如 短信 / 短信和邮箱")
    send_channel: str = Field(default="", description="同 invite_channel；优先于 invite_channel")
    invite_subject: str = Field(default="背调采集邀请")
    send_time: str = Field(default="", description="发送时间锚点，如 今天上午")
    phone_tail: str = Field(default="", description="手机尾号四位")
    reserved_email: str = Field(default="", description="预留邮箱（配邮件时）")
    has_email: bool | None = Field(default=None, description="是否配置授权邮件渠道")
    has_fallback_sms: bool | None = Field(default=None, description="是否配置兜底短信")
    fallback_sms_body: str = Field(default="", description="兜底短信正文（公众号指引唯一信息源）")
    mp_name: str = Field(default="", description="公众号名称")
    resend_channel: str = Field(default="短信", description="补发提醒渠道")
    deadline: str = Field(default="一个工作日内")
    hotline: str = Field(default="", description="答疑回拨号码")
    work_hours: str = Field(default="9:00–18:00")
    ticket_id: str = Field(default="", description="工单号（身份核验可选）")
    extra_note: str = Field(default="", description="额外备注，注入剧本")
    display: str = Field(
        default="",
        description="外显号（用户来电显示）；空则用 SIP_DEFAULT_DISPLAY / 号池第一个",
    )
    dry_run: bool | None = Field(
        default=None,
        description="true=只建任务不下外呼；默认跟随 OUTBOUND_DRY_RUN",
    )
    voice: str = Field(
        default="",
        description="豆包 TTS voice_type；空则用外呼默认音色。见 GET /outbound/v1/voices",
    )


class CompleteCallRequest(BaseModel):
    """Internal/demo: mark call finished with business result (simulates AI hangup)."""

    result: str = Field(..., description="业务结果标签，如 completed_auth_pending")
    result_summary: str = ""
    transcript: list[dict[str, str]] = Field(default_factory=list)


def _auth(x_api_key: str | None) -> None:
    if not verify_partner_api_key(x_api_key):
        raise HTTPException(status_code=401, detail="Invalid or missing X-API-Key")


@router.get("/scenarios")
async def list_scenarios(x_api_key: str | None = Header(default=None, alias="X-API-Key")) -> dict[str, Any]:
    _auth(x_api_key)
    return {
        "scenarios": [
            {
                "id": "urge_auth",
                "name": "催授权",
                "description": "催授权外呼：情景话术约束见 config/urge_auth_prompt.py（剧本 v2.3 冒烟口径）",
            },
            {
                "id": "tmp_prod_auth_demo",
                "name": "tmp-prod 催授权演示",
                "description": "临时演示剧本：config/tmp_prod_prompt.py（芝麻背调自称）",
            },
        ]
    }


@router.get("/dial-options")
async def dial_options(x_api_key: str | None = Header(default=None, alias="X-API-Key")) -> dict[str, Any]:
    """Web / 联调页：外显号池与默认值（来自 config.json outbound + env）。"""
    _auth(x_api_key)
    cfg = load_dial_settings()
    traffic: dict[str, Any] = {}
    try:
        from backend.sip.traffic import get_traffic_gate, load_sip_peers

        traffic = {
            **get_traffic_gate().snapshot(),
            "peer_pool": [{"host": h, "port": p} for h, p in load_sip_peers()],
        }
    except Exception as exc:  # noqa: BLE001
        traffic = {"error": str(exc)}
    voices = list_voice_catalog()
    return {
        "called_prefix": cfg.called_prefix,
        "ani": cfg.ani,
        "default_display": cfg.default_display,
        "display_numbers": list(cfg.display_numbers),
        "scenario": "urge_auth",
        "default_voice": voices.get("default_voice"),
        "traffic": traffic,
    }


@router.get("/voices")
async def list_voices(x_api_key: str | None = Header(default=None, alias="X-API-Key")) -> dict[str, Any]:
    """PM / 联调：豆包音色目录（S2S-Omni / SC 2.0 / O 2.0）。"""
    _auth(x_api_key)
    return list_voice_catalog()


@router.post("/preview-opening")
async def preview_opening(
    req: CreateCallRequest,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict[str, Any]:
    """联调：根据剧本槽位预览本通开场白（不落库、不外呼）。"""
    _auth(x_api_key)
    payload: dict[str, Any] = {
        "candidate_name": req.candidate_name,
        "employer": req.employer,
        "gender": req.gender,
        "phone": req.phone,
        "client_name": req.client_name,
        "identity_mode": req.identity_mode,
        "our_brand_name": req.our_brand_name,
        "channel_brand_name": req.channel_brand_name,
        "invite_channel": req.send_channel or req.invite_channel,
        "send_channel": req.send_channel or req.invite_channel,
        "invite_subject": req.invite_subject,
        "send_time": req.send_time,
        "phone_tail": req.phone_tail,
        "reserved_email": req.reserved_email,
        "deadline": req.deadline,
        "hotline": req.hotline,
        "work_hours": req.work_hours,
        "ticket_id": req.ticket_id,
        "extra_note": req.extra_note,
        "fallback_sms_body": req.fallback_sms_body,
        "mp_name": req.mp_name,
        "resend_channel": req.resend_channel,
    }
    if req.has_email is not None:
        payload["has_email"] = req.has_email
    if req.has_fallback_sms is not None:
        payload["has_fallback_sms"] = req.has_fallback_sms
    try:
        _role, _style, opening, parts = build_scenario_prompt(req.scenario, payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"scenario": req.scenario, "opening_line": opening, "opening_parts": parts}


@router.post("/calls")
async def create_call(
    req: CreateCallRequest,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict[str, Any]:
    """合作方发起一通 AI 外呼。立即返回 call_id；结束后通过轮询或 callback_url 取结果。"""
    _auth(x_api_key)
    display = normalize_phone(req.display)
    if display:
        pool = {normalize_phone(x) for x in load_dial_settings().display_numbers}
        if display not in pool:
            raise HTTPException(
                status_code=400,
                detail=f"display must be one of config outbound.display_numbers; got {display}",
            )
    payload: dict[str, Any] = {
        "candidate_name": req.candidate_name,
        "employer": req.employer,
        "gender": req.gender,
        "phone": req.phone,
        "client_name": req.client_name,
        "identity_mode": req.identity_mode,
        "our_brand_name": req.our_brand_name,
        "channel_brand_name": req.channel_brand_name,
        "invite_channel": req.send_channel or req.invite_channel,
        "send_channel": req.send_channel or req.invite_channel,
        "invite_subject": req.invite_subject,
        "send_time": req.send_time,
        "phone_tail": req.phone_tail,
        "reserved_email": req.reserved_email,
        "fallback_sms_body": req.fallback_sms_body,
        "mp_name": req.mp_name,
        "resend_channel": req.resend_channel,
        "deadline": req.deadline,
        "hotline": req.hotline,
        "work_hours": req.work_hours,
        "ticket_id": req.ticket_id,
        "extra_note": req.extra_note,
    }
    if req.has_email is not None:
        payload["has_email"] = req.has_email
    if req.has_fallback_sms is not None:
        payload["has_fallback_sms"] = req.has_fallback_sms
    try:
        voice = resolve_voice(req.voice)
        task = await create_outbound_call(
            phone=req.phone,
            scenario=req.scenario,
            external_id=req.external_id,
            callback_url=req.callback_url,
            payload=payload,
            dry_run=req.dry_run,
            display=display,
            voice=voice,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    dial = (task.telephony or {}).get("dial")
    opening_line = getattr(task, "opening_line", "") or ""
    return {
        "call_id": task.call_id,
        "status": task.status,
        "scenario": task.scenario,
        "voice": task.voice or None,
        "external_id": task.external_id or None,
        "dry_run": task.dry_run,
        "opening_line": opening_line or None,
        "dial": dial,
        "poll_url": f"/outbound/v1/calls/{task.call_id}",
        "message": (
            "任务已创建（dry_run，未真实外呼）。查看 dial 字段确认 a1/外显；可用 POST .../complete 模拟结果。"
            if task.dry_run
            else "已提交 SIP 外呼（被叫加 a1）；接通后 Voice Agent 对话。请轮询 poll_url 或等待 callback_url。"
        ),
    }


@router.get("/calls/{call_id}")
async def get_call(
    call_id: str,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict[str, Any]:
    """合作方查询通话状态与结果。"""
    _auth(x_api_key)
    task = get_call_store().get(call_id)
    if not task:
        raise HTTPException(status_code=404, detail="call_id not found")
    return task.to_public_dict()


@router.get("/calls/{call_id}/recording/{kind}")
async def get_call_recording(
    call_id: str,
    kind: str,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> FileResponse:
    """下载本通录音：stereo（L=用户 R=AI）/ user / ai。"""
    _auth(x_api_key)
    kind = (kind or "").strip().lower().removesuffix(".wav")
    if kind not in {"stereo", "user", "ai"}:
        raise HTTPException(status_code=400, detail="kind must be stereo|user|ai")
    task = get_call_store().get(call_id)
    if not task:
        raise HTTPException(status_code=404, detail="call_id not found")
    sip = ((task.telephony or {}).get("sip") or {})
    recordings = sip.get("recordings") or {}
    path = str(recordings.get(kind) or "").strip()
    if not path:
        raise HTTPException(status_code=404, detail=f"recording '{kind}' not available")
    from pathlib import Path

    file_path = Path(path)
    if not file_path.is_file():
        raise HTTPException(status_code=404, detail=f"recording file missing: {kind}")
    return FileResponse(
        file_path,
        media_type="audio/wav",
        filename=f"{call_id}_{kind}.wav",
    )


@router.get("/calls")
async def list_calls(
    limit: int = 20,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict[str, Any]:
    _auth(x_api_key)
    items = get_call_store().list_recent(limit)
    return {"calls": [t.to_public_dict() for t in items]}


@router.post("/calls/{call_id}/complete")
async def complete_call_demo(
    call_id: str,
    req: CompleteCallRequest,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict[str, Any]:
    """
    Demo/联调：手动回填通话结果并触发 callback_url。
    生产环境由语音 AI 挂机后自动调用同等逻辑。
    """
    _auth(x_api_key)
    if not get_call_store().get(call_id):
        raise HTTPException(status_code=404, detail="call_id not found")
    task = await complete_call(
        call_id,
        result=req.result or RESULT_ALREADY_DONE,
        result_summary=req.result_summary,
        transcript=req.transcript,
    )
    if not task:
        raise HTTPException(status_code=404, detail="call_id not found")
    return task.to_public_dict()


@router.get("/calls/{call_id}/prompt")
async def get_call_prompt(
    call_id: str,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict[str, Any]:
    """调试用：查看本通注入的催授权 system_role（勿暴露给外部生产环境时可关）。"""
    _auth(x_api_key)
    if (os.environ.get("OUTBOUND_EXPOSE_PROMPT", "") or "").strip().lower() not in {
        "1",
        "true",
        "yes",
        "on",
    }:
        raise HTTPException(status_code=403, detail="Set OUTBOUND_EXPOSE_PROMPT=true to enable")
    task = get_call_store().get(call_id)
    if not task:
        raise HTTPException(status_code=404, detail="call_id not found")
    return {
        "call_id": task.call_id,
        "scenario": task.scenario,
        "system_role": task.system_role,
        "speaking_style": task.speaking_style,
        "opening_line": getattr(task, "opening_line", "") or "",
        "supported_scenarios": sorted(SUPPORTED_SCENARIOS),
        "status": task.status if task.status != STATUS_COMPLETED else task.status,
    }
