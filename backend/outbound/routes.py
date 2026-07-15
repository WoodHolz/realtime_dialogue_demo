# -*- coding: utf-8 -*-
"""Partner-facing REST API for AI outbound calls (催授权场景)."""
from __future__ import annotations

import logging
import os
from typing import Any

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field

from backend.outbound.models import RESULT_ALREADY_DONE, STATUS_COMPLETED
from backend.outbound.service import (
    SUPPORTED_SCENARIOS,
    complete_call,
    create_outbound_call,
    verify_partner_api_key,
)
from backend.outbound.store import get_call_store

LOG = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["partner-outbound"])


class CreateCallRequest(BaseModel):
    phone: str = Field(..., description="被叫手机号（系统会自动加 a1 前缀）")
    scenario: str = Field(default="urge_auth", description="目前仅支持 urge_auth（催授权）")
    external_id: str = Field(default="", description="合作方侧业务单号，原样回传")
    callback_url: str = Field(default="", description="通话结束后 POST 结果到此地址")
    candidate_name: str = Field(default="", description="候选人称呼，如 张先生")
    client_name: str = Field(default="", description="委托方（候选人认识的颗粒度，如 闪购）")
    invite_channel: str = Field(default="手机短信和邮箱")
    invite_subject: str = Field(default="背调采集邀请")
    deadline: str = Field(default="一个工作日内")
    hotline: str = Field(default="", description="答疑回拨号码")
    extra_note: str = Field(default="", description="额外备注，注入剧本")
    display: str = Field(
        default="",
        description="外显号（用户来电显示）；空则用 RLCC_DEFAULT_DISPLAY / 号池第一个",
    )
    dry_run: bool | None = Field(
        default=None,
        description="true=只建任务不下外呼；默认跟随 OUTBOUND_DRY_RUN / RLCC 配置",
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
                "description": "芝麻背调 A1 催授权外呼，依据 bg_check_voice_docs 阶段2催授权剧本",
            }
        ]
    }


@router.post("/calls")
async def create_call(
    req: CreateCallRequest,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict[str, Any]:
    """合作方发起一通 AI 外呼。立即返回 call_id；结束后通过轮询或 callback_url 取结果。"""
    _auth(x_api_key)
    payload = {
        "candidate_name": req.candidate_name,
        "client_name": req.client_name,
        "invite_channel": req.invite_channel,
        "invite_subject": req.invite_subject,
        "deadline": req.deadline,
        "hotline": req.hotline,
        "extra_note": req.extra_note,
    }
    try:
        task = await create_outbound_call(
            phone=req.phone,
            scenario=req.scenario,
            external_id=req.external_id,
            callback_url=req.callback_url,
            payload=payload,
            dry_run=req.dry_run,
            display=req.display,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    dial = (task.telephony or {}).get("dial")
    return {
        "call_id": task.call_id,
        "status": task.status,
        "scenario": task.scenario,
        "external_id": task.external_id or None,
        "dry_run": task.dry_run,
        "dial": dial,
        "poll_url": f"/api/v1/calls/{task.call_id}",
        "message": (
            "任务已创建（dry_run，未真实外呼）。查看 dial 字段确认 a1/外显；可用 POST .../complete 模拟结果。"
            if task.dry_run
            else "已按容联拨号规则提交外呼（被叫加 a1、外显/主叫）；请轮询 poll_url 或等待 callback_url。"
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
        "supported_scenarios": sorted(SUPPORTED_SCENARIOS),
        "status": task.status if task.status != STATUS_COMPLETED else task.status,
    }
