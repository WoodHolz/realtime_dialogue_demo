# -*- coding: utf-8 -*-
"""Outbound call orchestration: partner request → telephony → AI scenario → result callback.

Telephony backend is selected by TELEPHONY_MODE (sip | rlcc); scenarios only supply prompts.
"""
from __future__ import annotations

import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Any

import httpx

from backend.outbound.models import (
    STATUS_COMPLETED,
    STATUS_DIALING,
    STATUS_FAILED,
    STATUS_IN_CALL,
    STATUS_QUEUED,
    RESULT_FAILED,
    RESULT_NO_ANSWER,
    CallTask,
)
from backend.outbound.store import CallStore, get_call_store
from config.urge_auth_prompt import (
    DEFAULT_SPEAKING_STYLE,
    build_urge_auth_system_role,
    urge_auth_slots_from_payload,
)

LOG = logging.getLogger(__name__)

SUPPORTED_SCENARIOS = {"urge_auth"}


def _partner_api_key() -> str:
    return (os.environ.get("PARTNER_API_KEY", "") or "").strip()


def _force_dry_run() -> bool:
    v = (os.environ.get("OUTBOUND_DRY_RUN", "") or "").strip().lower()
    return v in {"1", "true", "yes", "on"}


def verify_partner_api_key(provided: str | None) -> bool:
    expected = _partner_api_key()
    if not expected:
        # Dev-friendly: empty key means open (log once per process via caller).
        return True
    return (provided or "").strip() == expected


def build_scenario_prompt(scenario: str, payload: dict[str, Any]) -> tuple[str, str]:
    if scenario != "urge_auth":
        raise ValueError(f"Unsupported scenario: {scenario}")
    slots = urge_auth_slots_from_payload(payload)
    role = build_urge_auth_system_role(**slots)
    return role, DEFAULT_SPEAKING_STYLE


async def create_outbound_call(
    *,
    phone: str,
    scenario: str = "urge_auth",
    external_id: str = "",
    callback_url: str = "",
    payload: dict[str, Any] | None = None,
    dry_run: bool | None = None,
    display: str = "",
    store: CallStore | None = None,
) -> CallTask:
    phone = (phone or "").strip()
    scenario = (scenario or "urge_auth").strip()
    if not phone:
        raise ValueError("phone is required")
    if scenario not in SUPPORTED_SCENARIOS:
        raise ValueError(f"Unsupported scenario: {scenario}. Supported: {sorted(SUPPORTED_SCENARIOS)}")

    body = dict(payload or {})
    if display:
        body["display"] = display.strip()
    system_role, speaking_style = build_scenario_prompt(scenario, body)
    store = store or get_call_store()

    use_dry = _force_dry_run() if dry_run is None else bool(dry_run)
    task = CallTask(
        call_id=uuid.uuid4().hex,
        scenario=scenario,
        phone=phone,
        status=STATUS_QUEUED,
        external_id=(external_id or "").strip(),
        callback_url=(callback_url or "").strip(),
        payload=body,
        system_role=system_role,
        speaking_style=speaking_style,
        dry_run=use_dry,
    )
    store.put(task)

    # Always attach resolved dial plan for inspection (even dry_run).
    try:
        from backend.rlcc.config import load_rlcc_config
        from backend.rlcc.dial import build_dial_plan

        cfg = load_rlcc_config()
        dial = build_dial_plan(
            phone,
            called_prefix=cfg.called_prefix,
            ani=cfg.ani,
            display=str(body.get("display") or ""),
            default_display=cfg.default_display,
            display_pool=cfg.display_numbers,
            caller_mode=cfg.caller_mode,
        )
        store.update(task.call_id, telephony={"dial": dial.as_dict()})
        task = store.get(task.call_id) or task
    except Exception:
        LOG.exception("failed to build dial plan for call_id=%s", task.call_id)

    if use_dry:
        LOG.info(
            "outbound dry_run call_id=%s phone=%s dial=%s",
            task.call_id,
            phone,
            (task.telephony or {}).get("dial"),
        )
        return task

    # TELEPHONY_MODE=sip (default) | rlcc — see backend/sip/dialer.py
    from backend.sip.dialer import dispatch_outbound_call

    await dispatch_outbound_call(task, store)
    return store.get(task.call_id) or task


async def _dispatch_to_rlcc(task: CallTask, store: CallStore) -> None:
    from backend.rlcc.client import RlccClient
    from backend.rlcc.config import load_rlcc_config

    cfg = load_rlcc_config()
    if not cfg.is_configured():
        store.update(
            task.call_id,
            status=STATUS_FAILED,
            result=RESULT_FAILED,
            error=(
                "RLCC not configured; set RLCC_ENABLED=true and "
                "RLCC_CONNECTOR_USERNAME/PASSWORD + RLCC_API_BASE (or url_3cs), "
                "or OUTBOUND_DRY_RUN=true"
            ),
            finished_at=datetime.now(timezone.utc).isoformat(),
        )
        updated = store.get(task.call_id)
        if updated:
            await notify_partner_callback(updated)
        return

    store.update(task.call_id, status=STATUS_DIALING)
    client = RlccClient(cfg)
    display = str((task.payload or {}).get("display") or "")
    user_data = {
        "call_id": task.call_id,
        "scenario": task.scenario,
        "external_id": task.external_id,
        "phone_raw": task.phone,
        "ani": cfg.ani,
    }
    import json

    try:
        raw = await client.make_call(
            task.phone,
            display=display,
            user_data=json.dumps(user_data, ensure_ascii=False),
            apply_dial_plan=True,
        )
        dial_meta = raw.pop("_dial", None) if isinstance(raw, dict) else None
        tel = dict(task.telephony or {})
        if dial_meta:
            tel["dial"] = dial_meta
        tel["dispatch"] = raw
        store.update(task.call_id, rlcc_raw=raw, telephony=tel)
        LOG.info("RLCC makecall ok call_id=%s dial=%s raw=%s", task.call_id, dial_meta, raw)
    except Exception as exc:
        LOG.exception("RLCC makecall failed call_id=%s", task.call_id)
        store.update(
            task.call_id,
            status=STATUS_FAILED,
            result=RESULT_FAILED,
            error=str(exc),
            finished_at=datetime.now(timezone.utc).isoformat(),
        )
        updated = store.get(task.call_id)
        if updated:
            await notify_partner_callback(updated)


def mark_in_call(call_id: str, telephony: dict[str, Any] | None = None) -> CallTask | None:
    store = get_call_store()
    task = store.get(call_id)
    if not task:
        return None
    tel = dict(task.telephony or {})
    if telephony:
        tel.update(telephony)
    return store.update(call_id, status=STATUS_IN_CALL, telephony=tel)


async def complete_call(
    call_id: str,
    *,
    result: str,
    result_summary: str = "",
    transcript: list[dict[str, str]] | None = None,
    telephony: dict[str, Any] | None = None,
    error: str = "",
) -> CallTask | None:
    store = get_call_store()
    task = store.get(call_id)
    if not task:
        return None
    tel = dict(task.telephony or {})
    if telephony:
        tel.update(telephony)
    fields: dict[str, Any] = {
        "status": STATUS_COMPLETED if not error else STATUS_FAILED,
        "result": result,
        "result_summary": result_summary,
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "telephony": tel,
    }
    if transcript is not None:
        fields["transcript"] = transcript
    if error:
        fields["error"] = error
        fields["status"] = STATUS_FAILED
    updated = store.update(call_id, **fields)
    if updated:
        await notify_partner_callback(updated)
    return updated


async def notify_partner_callback(task: CallTask) -> None:
    url = (task.callback_url or "").strip()
    if not url:
        return
    body = {
        "event": "call.finished",
        "data": task.to_public_dict(),
    }
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(url, json=body)
            LOG.info(
                "partner callback call_id=%s status=%s url=%s",
                task.call_id,
                resp.status_code,
                url,
            )
    except Exception:
        LOG.exception("partner callback failed call_id=%s url=%s", task.call_id, url)


def apply_rlcc_event(event: dict[str, Any]) -> CallTask | None:
    """Best-effort map CONNECTOR webhook → task status by call_id in userData / payload."""
    store = get_call_store()
    payload = event.get("payload") if "payload" in event else event
    if not isinstance(payload, dict):
        return None

    call_id = _extract_call_id(payload)
    if not call_id:
        return None
    task = store.get(call_id)
    if not task:
        return None

    msg = payload.get("message") if isinstance(payload.get("message"), dict) else payload
    etype = str((msg or {}).get("type") or payload.get("type") or "").lower()
    tel = dict(task.telephony or {})
    tel["last_event"] = payload

    if etype in {"incall", "outboundcall", "answered"}:
        return store.update(call_id, status=STATUS_IN_CALL, telephony=tel)
    if etype in {"hangup", "agenthangup", "release"}:
        # If AI pipeline already finalized, keep result; else mark completed/no_answer placeholder.
        if task.result:
            return store.update(call_id, telephony=tel)
        return store.update(
            call_id,
            status=STATUS_COMPLETED,
            result=task.result or RESULT_NO_ANSWER,
            result_summary=task.result_summary or "通话结束，业务结果待 AI 汇总",
            telephony=tel,
            finished_at=task.finished_at or datetime.now(timezone.utc).isoformat(),
        )
    if etype in {"outringing", "makecall", "alerting"}:
        return store.update(call_id, status=STATUS_DIALING, telephony=tel)
    return store.update(call_id, telephony=tel)


def _extract_call_id(payload: dict[str, Any]) -> str:
    for key in ("call_id", "callId"):
        v = payload.get(key)
        if isinstance(v, str) and v:
            return v
    user_data = payload.get("userData") or payload.get("userdata")
    if isinstance(user_data, str) and user_data.strip().startswith("{"):
        try:
            import json

            data = json.loads(user_data)
            if isinstance(data, dict) and data.get("call_id"):
                return str(data["call_id"])
        except Exception:
            pass
    if isinstance(user_data, dict) and user_data.get("call_id"):
        return str(user_data["call_id"])
    msg = payload.get("message")
    if isinstance(msg, dict):
        return _extract_call_id(msg)
    return ""
