# -*- coding: utf-8 -*-
"""FastAPI routes for 容联云 RLCC integration."""
from __future__ import annotations

import logging
from collections import deque
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from backend.rlcc.client import RlccClient
from backend.rlcc.config import RlccConfig, load_rlcc_config

LOG = logging.getLogger(__name__)

router = APIRouter(prefix="/api/rlcc", tags=["rlcc"])

_call_events: deque[dict[str, Any]] = deque(maxlen=200)
_forkstream_sessions: dict[str, dict[str, Any]] = {}


def _client(cfg: RlccConfig | None = None) -> RlccClient:
    config = cfg or load_rlcc_config()
    if not config.is_configured():
        raise HTTPException(
            status_code=503,
            detail="RLCC not configured. Set RLCC_ENABLED=true and required env vars (see .env.example).",
        )
    return RlccClient(config)


class ConnectorRequest(BaseModel):
    cmd: str
    payload: dict[str, Any] = Field(default_factory=dict)


class OutboundRequest(BaseModel):
    called: str
    caller: str = ""
    user_data: str = ""
    agent: str = ""


class ForkSubscribeRequest(BaseModel):
    operators: str
    abbreviate: str = ""


@router.get("/config")
async def rlcc_public_config() -> dict[str, Any]:
    cfg = load_rlcc_config()
    return cfg.public_dict()


@router.get("/events")
async def rlcc_recent_events(limit: int = 50) -> dict[str, Any]:
    n = max(1, min(limit, 200))
    items = list(_call_events)[-n:]
    return {"events": items}


@router.post("/connector")
async def rlcc_connector_proxy(req: ConnectorRequest) -> dict[str, Any]:
    allowed = {
        "logon",
        "logout",
        "agentidle",
        "agentbusy",
        "agentstate",
        "makecall",
        "hangup",
        "callmute",
        "callunmute",
        "hold",
        "unhold",
    }
    if req.cmd not in allowed:
        raise HTTPException(status_code=400, detail=f"Unsupported cmd: {req.cmd}")
    client = _client()
    payload = {"cmd": req.cmd, **req.payload}
    cfg = client.config
    if "callBackUrl" not in payload and cfg.callback_base_url:
        payload["callBackUrl"] = cfg.callback_base_url.rstrip("/") + "/api/rlcc/webhook/call-events"
    try:
        result = await client.connector_command(payload)
    except Exception as exc:
        LOG.exception("RLCC connector %s failed", req.cmd)
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return result


@router.post("/outbound")
async def rlcc_outbound(req: OutboundRequest) -> dict[str, Any]:
    client = _client()
    try:
        result = await client.make_call(
            req.called,
            caller=req.caller,
            user_data=req.user_data,
            agent=req.agent,
        )
    except Exception as exc:
        LOG.exception("RLCC outbound failed")
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return result


@router.post("/forkstream/subscribe")
async def rlcc_fork_subscribe(req: ForkSubscribeRequest) -> dict[str, Any]:
    cfg = load_rlcc_config()
    if not cfg.forkstream_enabled:
        raise HTTPException(status_code=400, detail="RLCC_FORKSTREAM_ENABLED is false")
    client = _client(cfg)
    try:
        result = await client.subscribe_fork_stream(req.operators, abbreviate=req.abbreviate)
    except Exception as exc:
        LOG.exception("RLCC fork-stream subscribe failed")
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return result


@router.post("/forkstream/unsubscribe")
async def rlcc_fork_unsubscribe(req: ForkSubscribeRequest) -> dict[str, Any]:
    client = _client()
    try:
        result = await client.unsubscribe_fork_stream(req.operators, abbreviate=req.abbreviate)
    except Exception as exc:
        LOG.exception("RLCC fork-stream unsubscribe failed")
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return result


@router.post("/webhook/call-events")
async def rlcc_call_event_webhook(body: dict[str, Any]) -> dict[str, str]:
    """Receive CONNECTOR async callbacks (logon, makecall, incall, hangup, etc.)."""
    _call_events.append({"kind": "connector", "payload": body})
    LOG.info("RLCC call event: %s", body)
    try:
        from backend.outbound.service import apply_rlcc_event, notify_partner_callback

        task = apply_rlcc_event({"kind": "connector", "payload": body})
        if task and task.status in {"completed", "failed"} and task.callback_url:
            await notify_partner_callback(task)
    except Exception:
        LOG.exception("Failed to map RLCC event to outbound call task")
    return {"status": "ok"}


@router.post("/forkstream/on-call")
async def rlcc_forkstream_on_call(body: dict[str, Any]) -> dict[str, Any]:
    """
    Called by 容联云 when a subscribed agent enters a call.
    Respond with UDP endpoints for agent/user audio legs (fork-stream).
    """
    call_id = str(body.get("callId") or body.get("mgCallId") or "")
    _call_events.append({"kind": "forkstream_on_call", "payload": body})
    _forkstream_sessions[call_id] = body
    LOG.info("RLCC fork-stream on-call: %s", body)
    # Placeholder: production should bind local UDP ports and bridge to AI pipeline.
    return {
        "code": 0,
        "message": "accepted",
        "data": {
            "mgCallId": body.get("mgCallId", ""),
            "callerAddress": "127.0.0.1:0",
            "calledAddress": "127.0.0.1:0",
            "serviceUrl": (load_rlcc_config().callback_base_url or "").rstrip("/") + "/api/rlcc/forkstream/on-hangup",
        },
    }


@router.post("/forkstream/on-hangup")
async def rlcc_forkstream_on_hangup(body: dict[str, Any]) -> dict[str, str]:
    call_id = str(body.get("callId") or body.get("mgCallId") or "")
    _call_events.append({"kind": "forkstream_on_hangup", "payload": body})
    _forkstream_sessions.pop(call_id, None)
    LOG.info("RLCC fork-stream hangup: %s", body)
    return {"status": "ok"}
