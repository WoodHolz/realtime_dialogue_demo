# -*- coding: utf-8 -*-
"""Outbound call orchestration: partner request → SIP + Voice Agent → result callback."""
from __future__ import annotations

import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Any

import httpx

from backend.outbound.dial import build_dial_plan, load_dial_settings
from backend.outbound.models import (
    RESULT_FAILED,
    STATUS_FAILED,
    STATUS_QUEUED,
    CallTask,
)
from backend.outbound.store import CallStore, get_call_store
from config.urge_auth_prompt import (
    DEFAULT_SPEAKING_STYLE,
    build_urge_auth_opening,
    build_urge_auth_opening_parts,
    build_urge_auth_system_role,
    urge_auth_slots_from_payload,
)

LOG = logging.getLogger(__name__)

SUPPORTED_SCENARIOS = {"urge_auth", "guotou_auth_demo"}


def _partner_api_key() -> str:
    return (os.environ.get("PARTNER_API_KEY", "") or "").strip()


def _force_dry_run() -> bool:
    v = (os.environ.get("OUTBOUND_DRY_RUN", "") or "").strip().lower()
    return v in {"1", "true", "yes", "on"}


def verify_partner_api_key(provided: str | None) -> bool:
    expected = _partner_api_key()
    if not expected:
        return True
    return (provided or "").strip() == expected


def build_scenario_prompt(scenario: str, payload: dict[str, Any]) -> tuple[str, str, str, list[str]]:
    """Return (system_role, speaking_style, opening_line, opening_parts)."""
    if scenario == "guotou_auth_demo":
        from config.guotou_demo_prompt import (
            DEFAULT_SPEAKING_STYLE as G_STYLE,
            build_guotou_demo_opening,
            build_guotou_demo_opening_parts,
            build_guotou_demo_system_role,
            guotou_demo_slots_from_payload,
            stamp_trigger_beijing,
        )

        # Stamp trigger Beijing time at prompt build (= outbound trigger); persist for dialer rebuild.
        payload["trigger_beijing_at"] = stamp_trigger_beijing(payload)
        slots = guotou_demo_slots_from_payload(payload)
        role = build_guotou_demo_system_role(**slots)
        parts = build_guotou_demo_opening_parts(**slots)
        opening = build_guotou_demo_opening(**slots)
        return role, G_STYLE, opening, parts
    if scenario != "urge_auth":
        raise ValueError(f"Unsupported scenario: {scenario}")
    slots = urge_auth_slots_from_payload(payload)
    role = build_urge_auth_system_role(**slots)
    parts = build_urge_auth_opening_parts(**slots)
    opening = build_urge_auth_opening(**slots)
    return role, DEFAULT_SPEAKING_STYLE, opening, parts


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
    if scenario == "guotou_auth_demo":
        from config.guotou_demo_prompt import stamp_trigger_beijing

        # Freeze Beijing trigger time at call creation (本通外呼触发瞬间).
        body["trigger_beijing_at"] = stamp_trigger_beijing(body)
    system_role, speaking_style, opening_line, opening_parts = build_scenario_prompt(scenario, body)
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
        opening_line=opening_line,
        opening_parts=opening_parts,
        dry_run=use_dry,
    )
    store.put(task)

    try:
        dial_cfg = load_dial_settings()
        dial = build_dial_plan(
            phone,
            called_prefix=dial_cfg.called_prefix,
            ani=dial_cfg.ani,
            display=str(body.get("display") or ""),
            default_display=dial_cfg.default_display,
            display_pool=dial_cfg.display_numbers,
            caller_mode=dial_cfg.caller_mode,
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

    from backend.sip.dialer import dispatch_outbound_call

    await dispatch_outbound_call(task, store)
    return store.get(task.call_id) or task


def mark_in_call(call_id: str, telephony: dict[str, Any] | None = None) -> CallTask | None:
    from backend.outbound.models import STATUS_IN_CALL

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
    from backend.outbound.models import STATUS_COMPLETED

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
