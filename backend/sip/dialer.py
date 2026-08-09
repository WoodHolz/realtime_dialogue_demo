# -*- coding: utf-8 -*-
"""Outbound SIP dialer: INVITE + RTP ↔ Voice Agent bridge."""
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

RESULT_CONCURRENCY_LIMIT = "concurrency_limit"

# 404 + Q.850 cause=16 (NORMAL_CLEARING) is often a flaky carrier clear before answer.
SIP_CLEARING_404_RETRIES = 3
SIP_CLEARING_404_RETRY_INTERVAL_SEC = 5.0


def _is_retryable_404_normal_clearing(*, status: str, error: str) -> bool:
    """True for SIP 404 wrapped with Q.850 NORMAL_CLEARING (cause=16)."""
    if (status or "").strip() != "404":
        return False
    err = error or ""
    return "cause=16" in err or "NORMAL_CLEARING" in err


async def dispatch_outbound_call(task: CallTask, store: CallStore) -> None:
    """Start a background SIP + Agent call for the partner task."""
    from backend.sip.config import load_sip_config
    from backend.sip.traffic import get_traffic_gate

    cfg = load_sip_config()
    if not cfg.is_configured():
        store.update(
            task.call_id,
            status=STATUS_FAILED,
            result=RESULT_FAILED,
            error="SIP not configured; set SIP_SRC_IP / SIP_DST_HOST / SIP_FROM_USER",
            finished_at=datetime.now(timezone.utc).isoformat(),
        )
        updated = store.get(task.call_id)
        if updated:
            from backend.outbound.service import notify_partner_callback

            await notify_partner_callback(updated)
        return

    gate = get_traffic_gate()
    slot = await gate.try_acquire(task.call_id)
    if slot is None:
        snap = gate.snapshot()
        store.update(
            task.call_id,
            status=STATUS_FAILED,
            result=RESULT_CONCURRENCY_LIMIT,
            error=(
                f"outbound concurrency limit reached "
                f"(active={snap.get('active_total')}/{snap.get('max_concurrent')})"
            ),
            finished_at=datetime.now(timezone.utc).isoformat(),
            telephony={
                **(task.telephony or {}),
                "traffic": snap,
            },
        )
        updated = store.get(task.call_id)
        if updated:
            from backend.outbound.service import notify_partner_callback

            await notify_partner_callback(updated)
        return

    store.update(
        task.call_id,
        status=STATUS_DIALING,
        telephony={
            **(task.telephony or {}),
            "sip_peer": {"host": slot.host, "port": slot.port},
            "traffic": {
                "active_total": slot.active_total,
                "active_on_peer": slot.active_on_peer,
                "max_concurrent": gate.snapshot().get("max_concurrent"),
            },
        },
    )
    bg = asyncio.create_task(
        _run_sip_bridged_call(task.call_id, store, slot.host, slot.port),
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


async def _run_sip_bridged_call(
    call_id: str,
    store: CallStore,
    peer_host: str,
    peer_port: int,
) -> None:
    from backend.outbound.service import (
        complete_call,
        is_prod_scenario,
        is_reference_interview_scenario,
        notify_partner_callback,
    )
    from backend.sip.agent_bridge import SipAgentBridge
    from backend.sip.config import load_sip_config
    from backend.sip.traffic import get_traffic_gate

    gate = get_traffic_gate()
    try:
        task = store.get(call_id)
        if not task:
            return

        cfg = load_sip_config().with_peer(peer_host, peer_port)
        if is_prod_scenario(task.scenario) or is_reference_interview_scenario(
            task.scenario
        ):
            from dataclasses import replace
            from pathlib import Path

            if is_reference_interview_scenario(task.scenario):
                rec = Path(
                    os.environ.get("SIP_RECORD_DIR")
                    or "data/reference_interview/recordings"
                )
                max_sec = 720.0
            else:
                rec = Path(os.environ.get("SIP_RECORD_DIR") or "")
                if not str(rec):
                    from backend.prod.paths import prod_root

                    rec = prod_root() / "recordings"
                max_sec = 900.0
            rec.mkdir(parents=True, exist_ok=True)
            cfg = replace(cfg, max_call_sec=max_sec, record_dir=str(rec))
        dial = (task.telephony or {}).get("dial") or {}
        phone = str(dial.get("called") or task.phone)

        def on_status(status: str, extra: dict[str, Any]) -> None:
            cur = store.get(call_id) or task
            tel = dict(cur.telephony or {})
            sip_meta = {**(tel.get("sip") or {}), "status": status, **{
                k: v for k, v in extra.items()
                if k not in {"transcript", "turn_timings"}
            }}
            fields: dict[str, Any] = {}
            if status == "asr_interim":
                sip_meta["asr_interim"] = str(extra.get("text") or "")
            if status in {"transcript_update", "turn_latency"}:
                if "transcript" in extra:
                    fields["transcript"] = list(extra.get("transcript") or [])
                if "turn_timings" in extra:
                    fields["turn_timings"] = list(extra.get("turn_timings") or [])
                if "asr_interim" in extra:
                    sip_meta["asr_interim"] = str(extra.get("asr_interim") or "")
                elif status == "transcript_update":
                    sip_meta["asr_interim"] = ""
            tel["sip"] = sip_meta
            tel["sip_peer"] = {"host": peer_host, "port": peer_port}
            fields["telephony"] = tel
            if status == "answered":
                fields["status"] = STATUS_IN_CALL
            elif status == "dialing":
                fields["status"] = STATUS_DIALING
            store.update(call_id, **fields)

        opening = (getattr(task, "opening_line", None) or "").strip()
        parts = list(getattr(task, "opening_parts", None) or [])
        if not parts and opening:
            parts = [opening]
        system_role = task.system_role
        if cfg.ai_speak_first and opening and task.scenario == "urge_auth":
            from config.urge_auth_prompt import (
                build_urge_auth_system_role,
                urge_auth_slots_from_payload,
            )

            slots = urge_auth_slots_from_payload(task.payload)
            system_role = build_urge_auth_system_role(**slots, opening_preplayed=True)
        elif cfg.ai_speak_first and opening and is_prod_scenario(task.scenario):
            from config.prod_prompt import (
                build_prod_system_role,
                prod_slots_from_payload,
            )

            slots = prod_slots_from_payload(task.payload)
            system_role = build_prod_system_role(**slots, opening_preplayed=True)
        elif cfg.ai_speak_first and opening and is_reference_interview_scenario(
            task.scenario
        ):
            from config.reference_interview_prompt import (
                build_reference_interview_system_role,
                reference_interview_slots_from_payload,
            )

            slots = reference_interview_slots_from_payload(task.payload)
            system_role = build_reference_interview_system_role(
                **slots, opening_preplayed=True
            )

        if task.scenario == "urge_auth":
            bot_name = "催授权助手"
        elif is_reference_interview_scenario(task.scenario):
            bot_name = "背调访谈助理"
        else:
            bot_name = "语音助手"

        max_attempts = 1 + SIP_CLEARING_404_RETRIES
        dial_attempts: list[dict[str, Any]] = []
        outcome = None
        for attempt in range(1, max_attempts + 1):
            voice = (
                (getattr(task, "voice", None) or "").strip()
                or str((task.payload or {}).get("voice") or "")
                or str((task.payload or {}).get("voice_type") or "")
            )
            bridge = SipAgentBridge(
                cfg,
                system_role=system_role,
                speaking_style=task.speaking_style,
                call_id=call_id,
                bot_name=bot_name,
                opening_line=opening,
                opening_parts=parts,
                on_status=on_status,
                voice=voice,
                scenario=task.scenario or "",
            )
            try:
                outcome = await bridge.run(phone)
            except Exception as exc:
                LOG.exception(
                    "SIP bridged call crashed call_id=%s attempt=%s/%s",
                    call_id,
                    attempt,
                    max_attempts,
                )
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
            dial_attempts.append(
                {
                    "attempt": attempt,
                    "ok": sip.ok,
                    "status": sip.status,
                    "error": sip.error or outcome.error or "",
                    "sip_call_id": sip.sip_call_id,
                    "servercallid": sip.servercallid,
                }
            )
            if sip.ok:
                break
            retryable = _is_retryable_404_normal_clearing(
                status=sip.status or "",
                error=sip.error or outcome.error or "",
            )
            if not retryable or attempt >= max_attempts:
                break
            LOG.warning(
                "SIP 404 NORMAL_CLEARING call_id=%s attempt=%s/%s; retry in %.0fs error=%s",
                call_id,
                attempt,
                max_attempts,
                SIP_CLEARING_404_RETRY_INTERVAL_SEC,
                sip.error or outcome.error,
            )
            cur = store.get(call_id) or task
            tel_retry = dict(cur.telephony or {})
            tel_retry["sip"] = {
                **(tel_retry.get("sip") or {}),
                "status": "retrying",
                "error": sip.error or outcome.error,
                "dial_attempt": attempt,
                "dial_attempts": list(dial_attempts),
            }
            tel_retry["sip_peer"] = {"host": peer_host, "port": peer_port}
            store.update(call_id, status=STATUS_DIALING, telephony=tel_retry)
            await asyncio.sleep(SIP_CLEARING_404_RETRY_INTERVAL_SEC)

        assert outcome is not None
        sip = outcome.sip
        tel = dict((store.get(call_id) or task).telephony or {})
        tel["sip_peer"] = {"host": peer_host, "port": peer_port}
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
            "dst_host": peer_host,
            "dst_port": peer_port,
            "dial_attempt": len(dial_attempts),
            "dial_attempts": dial_attempts,
        }

        if not sip.ok:
            await complete_call(
                call_id,
                result=(
                    RESULT_NO_ANSWER
                    if "failed: 4" in (sip.error or "") or sip.status in {"404", "480", "486", "487"}
                    else RESULT_FAILED
                ),
                result_summary=sip.error or outcome.error or "SIP dial failed",
                transcript=outcome.transcript,
                turn_timings=outcome.turn_timings,
                telephony=tel,
                error=sip.error or outcome.error,
            )
            _persist_call_analytics(
                outcome,
                call_id=call_id,
                scenario=task.scenario,
                business_result=(
                    RESULT_NO_ANSWER
                    if "failed: 4" in (sip.error or "") or sip.status in {"404", "480", "486", "487"}
                    else RESULT_FAILED
                ),
                connected=False,
            )
            return

        summary = "通话结束"
        if outcome.transcript:
            summary = f"通话结束，共 {len(outcome.transcript)} 条转写"
        result_code = "completed"
        if is_prod_scenario(task.scenario):
            from backend.prod.outcome import extract_prod_outcome

            payload = task.payload if isinstance(task.payload, dict) else {}
            extracted = extract_prod_outcome(
                outcome.transcript,
                fallback_result="completed",
                fallback_summary=summary,
                trigger_beijing_at=payload.get("trigger_beijing_at") or "",
            )
            result_code = extracted.get("result") or "completed"
            summary = extracted.get("result_summary") or summary
            # Stash structured fields for batch / partner consumers.
            tel["business"] = {
                "result": result_code,
                "result_summary": summary,
                "email": extracted.get("email") or "",
                "promised_time": extracted.get("promised_time") or "",
            }
        await complete_call(
            call_id,
            result=result_code,
            result_summary=summary,
            transcript=outcome.transcript,
            turn_timings=outcome.turn_timings,
            telephony=tel,
            error="",
        )
        _persist_call_analytics(
            outcome,
            call_id=call_id,
            scenario=task.scenario,
            business_result=result_code,
            connected=True,
        )
    finally:
        await gate.release(peer_host, peer_port, call_id)


def _persist_call_analytics(
    outcome: Any,
    *,
    call_id: str,
    scenario: str,
    business_result: str,
    connected: bool,
) -> None:
    """Best-effort analytics write; never raise into dialer."""
    try:
        from backend.analytics.meter import CallUsageSession, record_call_analytics
        from backend.analytics.models import CallAnalyticsRecord
        from backend.analytics.funnel import hangup_party_from_reason, infer_hangup_stage

        snap = getattr(outcome, "analytics", None)
        sip = getattr(outcome, "sip", None)
        hangup_reason = ""
        if sip is not None:
            hangup_reason = getattr(sip, "hangup_reason", "") or ""
        transcript = list(getattr(outcome, "transcript", None) or [])

        if isinstance(snap, dict) and snap.get("call_id"):
            rec = CallAnalyticsRecord.from_dict(snap)
            rec.call_id = call_id or rec.call_id
            rec.scenario = scenario or rec.scenario
            rec.business_result = business_result or rec.business_result
            rec.connected = connected
            if not connected:
                rec.duration_sec = 0.0
            stage, confidence = infer_hangup_stage(
                scenario=rec.scenario,
                connected=connected,
                hangup_reason=hangup_reason or rec.hangup_reason,
                business_result=rec.business_result,
                transcript=transcript,
            )
            rec.hangup_reason = hangup_reason or rec.hangup_reason
            rec.hangup_party = hangup_party_from_reason(rec.hangup_reason)
            rec.hangup_stage = stage
            rec.hangup_stage_confidence = confidence
            record_call_analytics(rec)
            return

        session = CallUsageSession(call_id or "unknown", scenario=scenario)
        record_call_analytics(
            session.build_record(
                connected=connected,
                hangup_reason=hangup_reason,
                business_result=business_result,
                transcript=transcript,
            )
        )
    except Exception:
        LOG.exception("persist call analytics failed call_id=%s", call_id)
