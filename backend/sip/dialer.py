# -*- coding: utf-8 -*-
"""Outbound telephony dialer: SIP (+ Voice Agent) or RLCC CONNECTOR."""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone
from typing import Any

from backend.outbound.models import (
    RESULT_FAILED,
    RESULT_NO_ANSWER,
    STATUS_DIALING,
    STATUS_FAILED,
    STATUS_IN_CALL,
    CallTask,
)
from backend.outbound.store import CallStore

LOG = logging.getLogger(__name__)


def telephony_mode() -> str:
    """
    TELEPHONY_MODE:
      - sip (default): SIP INVITE + RTP + Voice Agent bridge
      - rlcc: CONNECTOR make_call (legacy / softphone path)
    """
    raw = (os.environ.get("TELEPHONY_MODE") or "sip").strip().lower()
    if raw in {"rlcc", "connector", "cc"}:
        return "rlcc"
    return "sip"


async def dispatch_outbound_call(task: CallTask, store: CallStore) -> None:
    mode = telephony_mode()
    if mode == "rlcc":
        from backend.outbound.service import _dispatch_to_rlcc

        await _dispatch_to_rlcc(task, store)
        return
    await _dispatch_to_sip(task, store)


async def _dispatch_to_sip(task: CallTask, store: CallStore) -> None:
    from backend.sip.config import load_sip_config

    cfg = load_sip_config()
    if not cfg.is_configured():
        store.update(
            task.call_id,
            status=STATUS_FAILED,
            result=RESULT_FAILED,
            error="SIP not configured; set SIP_SRC_IP / SIP_DST_HOST / SIP_FROM_USER (or RLCC_ANI)",
            finished_at=datetime.now(timezone.utc).isoformat(),
        )
        updated = store.get(task.call_id)
        if updated:
            from backend.outbound.service import notify_partner_callback

            await notify_partner_callback(updated)
        return

    store.update(task.call_id, status=STATUS_DIALING)
    # Fire-and-forget: HTTP create returns while call runs.
    bg = asyncio.create_task(
        _run_sip_bridged_call(task.call_id, store),
        name=f"sip-call-{task.call_id}",
    )

    def _log_bg(t: asyncio.Task) -> None:
        try:
            exc = t.exception()
        except asyncio.CancelledError:
            return
        if exc:
            LOG.error("SIP background task failed call_id=%s: %s", task.call_id, exc)

    bg.add_done_callback(_log_bg)


async def _run_sip_bridged_call(call_id: str, store: CallStore) -> None:
    from backend.outbound.service import complete_call, notify_partner_callback
    from backend.sip.agent_bridge import SipAgentBridge
    from backend.sip.config import load_sip_config

    task = store.get(call_id)
    if not task:
        return

    cfg = load_sip_config()
    dial = (task.telephony or {}).get("dial") or {}
    phone = str(dial.get("called") or task.phone)

    def on_status(status: str, extra: dict[str, Any]) -> None:
        tel = dict((store.get(call_id) or task).telephony or {})
        tel["sip"] = {**(tel.get("sip") or {}), "status": status, **extra}
        fields: dict[str, Any] = {"telephony": tel}
        if status == "answered":
            fields["status"] = STATUS_IN_CALL
        elif status == "dialing":
            fields["status"] = STATUS_DIALING
        store.update(call_id, **fields)

    bridge = SipAgentBridge(
        cfg,
        system_role=task.system_role,
        speaking_style=task.speaking_style,
        call_id=call_id,
        bot_name="催授权助手" if task.scenario == "urge_auth" else "语音助手",
        on_status=on_status,
    )

    try:
        outcome = await bridge.run(phone)
    except Exception as exc:
        LOG.exception("SIP bridged call crashed call_id=%s", call_id)
        store.update(
            call_id,
            status=STATUS_FAILED,
            result=RESULT_FAILED,
            error=str(exc),
            finished_at=datetime.now(timezone.utc).isoformat(),
        )
        updated = store.get(call_id)
        if updated:
            await notify_partner_callback(updated)
        return

    sip = outcome.sip
    tel = dict((store.get(call_id) or task).telephony or {})
    tel["sip"] = {
        **(tel.get("sip") or {}),
        "ok": sip.ok,
        "sip_call_id": sip.sip_call_id,
        "servercallid": sip.servercallid,
        "remote_rtp": list(sip.remote_rtp) if sip.remote_rtp else None,
        "hangup_reason": sip.hangup_reason,
        "rx_packets": sip.rx_packets,
        "tx_packets": sip.tx_packets,
        "status": sip.status,
        "error": sip.error or outcome.error,
        "recordings": outcome.recordings or None,
    }

    if not sip.ok:
        await complete_call(
            call_id,
            result=RESULT_NO_ANSWER if "failed: 4" in (sip.error or "") or sip.status in {"404", "480", "486", "487"} else RESULT_FAILED,
            result_summary=sip.error or outcome.error or "SIP dial failed",
            transcript=outcome.transcript,
            telephony=tel,
            error=sip.error or outcome.error,
        )
        return

    # Answered + media finished — business result tagging can be refined later from transcript.
    summary = "通话结束"
    if outcome.transcript:
        summary = f"通话结束，共 {len(outcome.transcript)} 轮用户发言"
    await complete_call(
        call_id,
        result="completed",
        result_summary=summary,
        transcript=outcome.transcript,
        telephony=tel,
        error="",
    )
