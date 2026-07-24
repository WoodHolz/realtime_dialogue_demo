# -*- coding: utf-8 -*-
"""tmp-prod batch dialer + harness enqueue (no long sleep for retry/reschedule)."""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from backend.tmp_prod.harness.models import CALL_KIND_INITIAL, CALL_KIND_RESCHEDULE
from backend.tmp_prod.harness.policy import MAX_ATTEMPTS, RETRY_DELAY_SEC, plan_next_action
from backend.tmp_prod.harness.store import get_action_store
from backend.tmp_prod.store import Batch, BatchStore, ContactJob, get_batch_store

LOG = logging.getLogger(__name__)

SCENARIO = "tmp_prod_auth_demo"
CALL_MAX_SEC = 900  # 15 min
POLL_SEC = 2.0


def batch_max_concurrent() -> int:
    """How many contacts to dial at once in a tmp-prod batch."""
    raw = (os.environ.get("TMP_PROD_MAX_CONCURRENT") or "").strip()
    if raw:
        try:
            return max(1, int(raw))
        except ValueError:
            pass
    from backend.sip.traffic import max_concurrent

    return max(1, max_concurrent())


def batch_concurrency_limit(override: int | None = None) -> int:
    """How many contacts to dial in parallel within one batch."""
    if override is not None and override > 0:
        return override
    raw = (os.environ.get("TMP_PROD_BATCH_CONCURRENT") or "").strip()
    if raw:
        try:
            return max(1, int(raw))
        except ValueError:
            pass
    # Fall back to global SIP outbound cap (default 60).
    try:
        from backend.sip.traffic import max_concurrent

        return max(1, int(max_concurrent()))
    except Exception:
        return 60


def ensure_tmp_prod_env() -> Path:
    """Ensure recordings land under data/tmp_prod/recordings."""
    root = Path(os.environ.get("TMP_PROD_ROOT", "data/tmp_prod"))
    rec = root / "recordings"
    rec.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("SIP_RECORD_DIR", str(rec))
    # Prefer 15-minute media cap for this demo process.
    os.environ.setdefault("SIP_MAX_CALL_SEC", str(CALL_MAX_SEC))
    return root


async def _wait_call_finished(call_id: str, *, timeout_sec: float = CALL_MAX_SEC + 120) -> Any:
    from backend.outbound.store import get_call_store

    store = get_call_store()
    deadline = asyncio.get_event_loop().time() + timeout_sec
    while asyncio.get_event_loop().time() < deadline:
        task = store.get(call_id)
        if task and task.status in {"completed", "failed"}:
            return task
        await asyncio.sleep(POLL_SEC)
    return get_call_store().get(call_id)


def _is_no_answer(task: Any) -> bool:
    if not task:
        return True
    result = (task.result or "").strip()
    if result in {"no_answer", "busy"}:
        return True
    err = task.error or ""
    if "failed: 4" in err:
        return True
    return False


def _dial_options_payload(
    *,
    identity_mode: str,
    client_name: str,
    our_brand_name: str,
    channel_brand_name: str,
    voice: str,
) -> dict[str, str]:
    return {
        "identity_mode": (identity_mode or "BRAND_SELF").strip() or "BRAND_SELF",
        "client_name": (client_name or "比赛日科技").strip() or "比赛日科技",
        "our_brand_name": (our_brand_name or "芝麻背调").strip() or "芝麻背调",
        "channel_brand_name": (channel_brand_name or "").strip(),
        "voice": (voice or "").strip(),
    }


def _enqueue_planned(
    *,
    batch_id: str,
    contact_id: str,
    planned: Any,
    source_call_id: str,
    dial_options: dict[str, str],
    store: BatchStore,
    extra_contact_fields: dict[str, Any] | None = None,
) -> None:
    """Persist NextAction and park contact until due_at (scheduler starts a new dial)."""
    action_store = get_action_store()
    action_store.cancel_pending_for_contact(batch_id, contact_id)
    action = action_store.create(
        batch_id=batch_id,
        contact_id=contact_id,
        type=planned.type,
        due_at=planned.due_at.astimezone(timezone.utc).isoformat(),
        reason=planned.reason,
        source_call_id=source_call_id,
        dial_options=dial_options,
        callback_context=dict(planned.callback_context or {}),
    )
    fields = {
        "status": planned.contact_status,
        "next_retry_at": action.due_at,
        "pending_action_id": action.action_id,
        "callback_context": dict(planned.callback_context or {}),
        "fail_reason": "",
        **(extra_contact_fields or {}),
    }
    store.update_contact(batch_id, contact_id, **fields)
    LOG.info(
        "tmp-prod harness enqueued type=%s action=%s due_at=%s contact=%s",
        planned.type,
        action.action_id,
        action.due_at,
        contact_id,
    )


async def dial_one_contact(
    batch_id: str,
    contact: ContactJob,
    *,
    store: BatchStore | None = None,
    identity_mode: str = "BRAND_SELF",
    client_name: str = "比赛日科技",
    our_brand_name: str = "芝麻背调",
    channel_brand_name: str = "",
    voice: str = "",
    call_kind: str = "",
    callback_context: dict[str, Any] | None = None,
) -> ContactJob:
    """Place one outbound attempt; enqueue harness actions on no-answer / reschedule."""
    ensure_tmp_prod_env()
    store = store or get_batch_store()
    attempt = contact.attempt + 1
    dial_opts = _dial_options_payload(
        identity_mode=identity_mode,
        client_name=client_name,
        our_brand_name=our_brand_name,
        channel_brand_name=channel_brand_name,
        voice=voice,
    )
    kind = (call_kind or "").strip() or CALL_KIND_INITIAL
    ctx = dict(callback_context or contact.callback_context or {})
    if kind == CALL_KIND_RESCHEDULE and not ctx.get("call_kind"):
        ctx["call_kind"] = CALL_KIND_RESCHEDULE

    store.update_contact(
        batch_id,
        contact.contact_id,
        status="dialing",
        attempt=attempt,
        fail_reason="",
        next_retry_at="",
        pending_action_id="",
    )

    from backend.outbound.service import create_outbound_call

    payload: dict[str, Any] = {
        "candidate_name": contact.name,
        "employer": contact.employer or "比赛日科技",
        "gender": contact.gender,
        "phone": contact.phone,
        "client_name": dial_opts["client_name"] or contact.employer or "比赛日科技",
        "identity_mode": dial_opts["identity_mode"],
        "our_brand_name": dial_opts["our_brand_name"],
        "channel_brand_name": dial_opts["channel_brand_name"],
        "call_kind": kind,
    }
    if ctx:
        payload["callback_context"] = ctx
        # Flatten a few fields for prompt builders / debugging
        if ctx.get("email") and not contact.email:
            payload["known_email"] = ctx.get("email")
        if ctx.get("promised_time"):
            payload["prior_promised_time"] = ctx.get("promised_time")

    task = await create_outbound_call(
        phone=contact.phone,
        scenario=SCENARIO,
        external_id=f"{batch_id}:{contact.contact_id}:{attempt}",
        payload=payload,
        voice=voice,
    )
    call_ids = list(contact.call_ids) + [task.call_id]
    store.update_contact(
        batch_id,
        contact.contact_id,
        last_call_id=task.call_id,
        call_ids=call_ids,
        status="in_call" if not task.dry_run else "dialing",
        callback_context=ctx,
    )

    if task.dry_run:
        updated = store.update_contact(
            batch_id,
            contact.contact_id,
            status="completed",
            result="dry_run",
            result_summary="dry_run：未真实外呼",
            last_call_id=task.call_id,
            call_ids=call_ids,
        )
        return updated or contact

    finished = await _wait_call_finished(task.call_id)
    transcript = list(getattr(finished, "transcript", None) or [])
    tel = dict(getattr(finished, "telephony", None) or {})
    sip = dict(tel.get("sip") or {})
    recordings = dict(sip.get("recordings") or {})
    stereo_path, recording_dir, recordings = _align_contact_recording(
        batch_id, contact, finished.call_id if finished else task.call_id, recordings
    )

    if finished:
        _persist_artifacts(batch_id, contact.contact_id, finished.call_id, transcript, recordings)

    rec_fields: dict[str, Any] = {
        "transcript": transcript,
        "recordings": recordings,
        "recording_dir": recording_dir,
        "stereo_path": stereo_path,
    }
    source_call_id = finished.call_id if finished else task.call_id

    if _is_no_answer(finished):
        planned = plan_next_action(
            outcome={"result": "no_answer"},
            attempt=attempt,
            max_attempts=MAX_ATTEMPTS,
            is_no_answer=True,
            source_call_id=source_call_id,
            retry_delay_sec=RETRY_DELAY_SEC,
        )
        if planned:
            _enqueue_planned(
                batch_id=batch_id,
                contact_id=contact.contact_id,
                planned=planned,
                source_call_id=source_call_id,
                dial_options=dial_opts,
                store=store,
                extra_contact_fields={
                    **rec_fields,
                    "result": "no_answer",
                    "result_summary": getattr(finished, "result_summary", "")
                    or planned.reason,
                },
            )
        else:
            store.update_contact(
                batch_id,
                contact.contact_id,
                status="failed",
                result="no_answer",
                result_summary="2次尝试仍未接通",
                fail_reason="未接通",
                next_retry_at="",
                pending_action_id="",
                **rec_fields,
            )
    else:
        from backend.tmp_prod.outcome import extract_tmp_prod_outcome

        fallback_result = getattr(finished, "result", None) or "completed"
        fallback_summary = getattr(finished, "result_summary", "") or ""
        call_payload = getattr(finished, "payload", None) or {}
        if not isinstance(call_payload, dict):
            call_payload = {}
        outcome = extract_tmp_prod_outcome(
            transcript,
            fallback_result=fallback_result,
            fallback_summary=fallback_summary,
            trigger_beijing_at=call_payload.get("trigger_beijing_at") or "",
        )
        err = getattr(finished, "error", "") or ""
        terminal_failed = getattr(finished, "status", "") == "failed"
        email = outcome.get("email") or contact.email or ""
        promised_time = outcome.get("promised_time") or ""

        # Even if SIP status=failed (e.g. NORMAL_CLEARING after a real talk), still
        # enqueue reschedule when outcome has a schedulable promised_time.
        planned = plan_next_action(
            outcome=outcome,
            attempt=attempt,
            max_attempts=MAX_ATTEMPTS,
            is_no_answer=False,
            source_call_id=source_call_id,
            contact_email=email,
        )
        if planned is None:
            LOG.info(
                "tmp-prod harness skip enqueue contact=%s result=%s promised=%r "
                "terminal_failed=%s transcript_turns=%s",
                contact.contact_id,
                outcome.get("result"),
                promised_time,
                terminal_failed,
                len(transcript),
            )

        if planned:
            _enqueue_planned(
                batch_id=batch_id,
                contact_id=contact.contact_id,
                planned=planned,
                source_call_id=source_call_id,
                dial_options=dial_opts,
                store=store,
                extra_contact_fields={
                    **rec_fields,
                    "result": outcome.get("result") or fallback_result,
                    "result_summary": outcome.get("result_summary") or fallback_summary,
                    "email": email,
                    "promised_time": promised_time,
                },
            )
        else:
            status = "failed" if terminal_failed else "completed"
            store.update_contact(
                batch_id,
                contact.contact_id,
                status=status,
                result=outcome.get("result") or fallback_result,
                result_summary=outcome.get("result_summary") or fallback_summary,
                email=email,
                promised_time=promised_time,
                fail_reason=err if status == "failed" else "",
                next_retry_at="",
                pending_action_id="",
                callback_context={},
                **rec_fields,
            )

    batch = store.get(batch_id)
    assert batch
    for c in batch.contacts:
        if c.contact_id == contact.contact_id:
            return c
    return contact


def _resolve_stereo_path(recordings: dict[str, str], recording_dir: str = "") -> str:
    stereo = str((recordings or {}).get("stereo") or "").strip()
    if stereo and Path(stereo).is_file():
        return stereo
    if recording_dir:
        candidate = Path(recording_dir) / "stereo.wav"
        if candidate.is_file():
            return str(candidate)
    for p in (recordings or {}).values():
        sib = Path(p).with_name("stereo.wav")
        if sib.is_file():
            return str(sib)
    return stereo


def _align_contact_recording(
    batch_id: str,
    contact: ContactJob,
    call_id: str,
    recordings: dict[str, str],
) -> tuple[str, str, dict[str, str]]:
    """
    Align one contact's dual-channel recording under the batch tree:

      data/tmp_prod/{batch_id}/contacts/{contact_id}/{call_id}/stereo.wav
    """
    import shutil

    root = Path(os.environ.get("TMP_PROD_ROOT", "data/tmp_prod"))
    dest_dir = root / batch_id / "contacts" / contact.contact_id / call_id
    dest_dir.mkdir(parents=True, exist_ok=True)

    recording_dir = ""
    if recordings:
        sample = next(iter(recordings.values()))
        recording_dir = str(Path(sample).parent)

    stereo_src = _resolve_stereo_path(recordings, recording_dir)
    aligned: dict[str, str] = dict(recordings or {})
    stereo_dest = dest_dir / "stereo.wav"

    if stereo_src and Path(stereo_src).is_file():
        try:
            shutil.copy2(stereo_src, stereo_dest)
        except Exception:
            LOG.exception("copy stereo failed call_id=%s", call_id)
            stereo_dest = Path(stereo_src)
    elif recording_dir:
        for name in ("stereo.wav", "user.wav", "ai.wav", "mixed.wav"):
            src = Path(recording_dir) / name
            if src.is_file():
                try:
                    shutil.copy2(src, dest_dir / name)
                except Exception:
                    LOG.debug("copy %s failed", name, exc_info=True)

    for key, src_path in list(aligned.items()):
        if key == "stereo":
            continue
        src = Path(src_path)
        if src.is_file():
            try:
                dst = dest_dir / src.name
                if not dst.exists():
                    shutil.copy2(src, dst)
                aligned[key] = str(dst)
            except Exception:
                LOG.debug("align copy %s failed", key, exc_info=True)

    if stereo_dest.is_file():
        aligned["stereo"] = str(stereo_dest)
        return str(stereo_dest), str(dest_dir), aligned
    if stereo_src:
        aligned["stereo"] = stereo_src
        return stereo_src, recording_dir or str(dest_dir), aligned
    return "", recording_dir or str(dest_dir), aligned


def _persist_artifacts(
    batch_id: str,
    contact_id: str,
    call_id: str,
    transcript: list[dict[str, str]],
    recordings: dict[str, str],
) -> None:
    root = Path(os.environ.get("TMP_PROD_ROOT", "data/tmp_prod")) / batch_id / "artifacts" / contact_id
    root.mkdir(parents=True, exist_ok=True)
    import json

    (root / f"{call_id}_transcript.json").write_text(
        json.dumps({"call_id": call_id, "transcript": transcript}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    if recordings:
        (root / f"{call_id}_recordings.json").write_text(
            json.dumps(recordings, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )


def _maybe_mark_batch_done(batch_id: str, store: BatchStore) -> None:
    batch = store.get(batch_id)
    if not batch:
        return
    open_statuses = {"pending", "dialing", "in_call", "waiting_retry", "waiting_callback"}
    if any(c.status in open_statuses for c in batch.contacts):
        return
    batch.status = "done"
    store.save(batch)


async def run_batch(
    batch_id: str,
    *,
    identity_mode: str = "BRAND_SELF",
    client_name: str = "比赛日科技",
    our_brand_name: str = "芝麻背调",
    channel_brand_name: str = "",
    stagger_sec: float = 0.5,
    max_concurrent: int | None = None,
    voice: str = "",
) -> Batch:
    """Dial batch contacts concurrently (capped); SIP traffic gate still applies."""
    ensure_tmp_prod_env()
    store = get_batch_store()
    batch = store.get(batch_id)
    if not batch:
        raise ValueError(f"batch not found: {batch_id}")
    batch.status = "running"
    store.save(batch)

    limit = max(1, int(max_concurrent if max_concurrent is not None else batch_max_concurrent()))
    sem = asyncio.Semaphore(limit)
    LOG.info(
        "tmp-prod batch start batch_id=%s contacts=%s max_concurrent=%s voice=%s",
        batch_id,
        len(batch.contacts),
        limit,
        voice or "(default)",
    )

    async def _one(contact_id: str) -> None:
        async with sem:
            batch_now = store.get(batch_id)
            if not batch_now:
                return
            contact = next((c for c in batch_now.contacts if c.contact_id == contact_id), None)
            if not contact or contact.status in {
                "completed",
                "failed",
                "waiting_retry",
                "waiting_callback",
            }:
                return
            try:
                await dial_one_contact(
                    batch_id,
                    contact,
                    store=store,
                    identity_mode=identity_mode,
                    client_name=client_name,
                    our_brand_name=our_brand_name,
                    channel_brand_name=channel_brand_name,
                    voice=voice,
                )
            except Exception as exc:
                LOG.exception("tmp-prod dial error contact=%s", contact_id)
                store.update_contact(
                    batch_id,
                    contact_id,
                    status="failed",
                    result="call_failed",
                    fail_reason=str(exc),
                    result_summary=str(exc),
                )

    tasks: list[asyncio.Task] = []
    for i, contact in enumerate(list(batch.contacts)):
        if contact.status in {"completed", "failed", "waiting_retry", "waiting_callback"}:
            continue
        tasks.append(asyncio.create_task(_one(contact.contact_id), name=f"tmp-prod-{contact.contact_id}"))
        if stagger_sec > 0 and i + 1 < len(batch.contacts):
            await asyncio.sleep(stagger_sec)

    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)

    _maybe_mark_batch_done(batch_id, store)
    out = store.get(batch_id)
    assert out
    return out


async def start_single_dial(
    *,
    phone: str,
    candidate_name: str = "",
    gender: str = "",
    employer: str = "",
    identity_mode: str = "BRAND_SELF",
    client_name: str = "比赛日科技",
    our_brand_name: str = "芝麻背调",
    channel_brand_name: str = "",
    voice: str = "",
) -> dict[str, Any]:
    """Create a 1-contact batch and dial via harness-aware dial_one_contact."""
    ensure_tmp_prod_env()
    phone = (phone or "").strip()
    if not phone:
        raise ValueError("phone is required")
    store = get_batch_store()
    batch = store.create_batch(
        [
            {
                "employer": (employer or client_name or "").strip(),
                "name": (candidate_name or "候选人").strip() or "候选人",
                "gender": (gender or "").strip(),
                "phone": phone,
            }
        ],
        source_name="single",
    )
    batch.status = "running"
    store.save(batch)
    contact = batch.contacts[0]

    async def _run() -> None:
        try:
            await dial_one_contact(
                batch.batch_id,
                contact,
                store=store,
                identity_mode=identity_mode,
                client_name=client_name,
                our_brand_name=our_brand_name,
                channel_brand_name=channel_brand_name,
                voice=voice,
            )
        except Exception as exc:
            LOG.exception("tmp-prod single dial failed batch=%s", batch.batch_id)
            store.update_contact(
                batch.batch_id,
                contact.contact_id,
                status="failed",
                result="call_failed",
                fail_reason=str(exc),
                result_summary=str(exc),
            )
        finally:
            _maybe_mark_batch_done(batch.batch_id, store)

    asyncio.create_task(_run(), name=f"tmp-prod-single-{batch.batch_id}")
    return {
        "batch_id": batch.batch_id,
        "contact_id": contact.contact_id,
        "status": "running",
        "phone": phone,
        "scenario": SCENARIO,
        "voice": voice,
        "message": "已开始单人外呼（走 harness：未接通重试 / 改约到点再拨）",
    }


def single_contact_snapshot(batch_id: str, contact_id: str | None = None) -> dict[str, Any] | None:
    """Merge contact + latest outbound call + harness actions for single-page polling."""
    store = get_batch_store()
    batch = store.get(batch_id)
    if not batch:
        return None
    contact = None
    if contact_id:
        contact = next((c for c in batch.contacts if c.contact_id == contact_id), None)
    if contact is None and batch.contacts:
        contact = batch.contacts[0]
    if contact is None:
        return None

    from backend.outbound.store import get_call_store
    from backend.tmp_prod.harness.store import get_action_store

    call_payload: dict[str, Any] = {}
    call_id = (contact.last_call_id or "").strip()
    if call_id:
        task = get_call_store().get(call_id)
        if task:
            call_payload = task.to_public_dict()

    actions = [a.to_dict() for a in get_action_store().list_actions(batch_id=batch_id)]
    pending = [
        a
        for a in actions
        if a.get("status") in {"pending", "running"}
        and a.get("contact_id") == contact.contact_id
    ]
    # Prefer live call transcript while in progress; fall back to contact copy.
    transcript = call_payload.get("transcript") or list(contact.transcript or [])
    status = str(call_payload.get("status") or contact.status)
    # Contact-level waiting_* is the source of truth between calls.
    if contact.status in {"waiting_retry", "waiting_callback"}:
        status = contact.status

    return {
        "batch_id": batch_id,
        "contact_id": contact.contact_id,
        "contact_status": contact.status,
        "batch_status": batch.status,
        "attempt": contact.attempt,
        "next_retry_at": contact.next_retry_at,
        "pending_action_id": contact.pending_action_id,
        "promised_time": contact.promised_time or "",
        "email": contact.email or "",
        "result": contact.result or call_payload.get("result") or "",
        "result_summary": contact.result_summary or call_payload.get("result_summary") or "",
        "call_id": call_id or call_payload.get("call_id") or "",
        "call_ids": list(contact.call_ids),
        "harness_actions": actions,
        "pending_harness": pending,
        "callback_context": dict(contact.callback_context or {}),
        "call": call_payload,
        "status": status,
        "transcript": transcript,
        "asr_interim": call_payload.get("asr_interim"),
        "recording_urls": call_payload.get("recording_urls"),
        "telephony": call_payload.get("telephony"),
        "error": call_payload.get("error") or contact.fail_reason,
        "scenario": SCENARIO,
        "opening_line": call_payload.get("opening_line") or "",
    }