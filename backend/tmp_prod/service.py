# -*- coding: utf-8 -*-
"""tmp-prod batch dialer: Guotou demo + 30min no-answer retry (max 2)."""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from backend.tmp_prod.store import Batch, BatchStore, ContactJob, get_batch_store

LOG = logging.getLogger(__name__)

SCENARIO = "guotou_auth_demo"
RETRY_DELAY_SEC = int(os.environ.get("TMP_PROD_RETRY_SEC", "1800"))  # 30 min
MAX_ATTEMPTS = 2
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


async def dial_one_contact(
    batch_id: str,
    contact: ContactJob,
    *,
    store: BatchStore | None = None,
    dry_run: bool | None = None,
) -> ContactJob:
    """Place one outbound attempt; schedule retry on no-answer if attempts remain."""
    ensure_tmp_prod_env()
    store = store or get_batch_store()
    attempt = contact.attempt + 1
    store.update_contact(
        batch_id,
        contact.contact_id,
        status="dialing",
        attempt=attempt,
        fail_reason="",
        next_retry_at="",
    )

    from backend.outbound.service import create_outbound_call

    payload = {
        "candidate_name": contact.name,
        "employer": contact.employer or "陕西榆林能源集团有限公司",
        "gender": contact.gender,
        "phone": contact.phone,
        "client_name": contact.employer,
    }
    task = await create_outbound_call(
        phone=contact.phone,
        scenario=SCENARIO,
        external_id=f"{batch_id}:{contact.contact_id}:{attempt}",
        payload=payload,
        dry_run=dry_run,
    )
    call_ids = list(contact.call_ids) + [task.call_id]
    store.update_contact(
        batch_id,
        contact.contact_id,
        last_call_id=task.call_id,
        call_ids=call_ids,
        status="in_call" if not task.dry_run else "dialing",
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

    # Persist transcript sidecar
    if finished:
        _persist_artifacts(batch_id, contact.contact_id, finished.call_id, transcript, recordings)

    rec_fields = {
        "transcript": transcript,
        "recordings": recordings,
        "recording_dir": recording_dir,
        "stereo_path": stereo_path,
    }

    if _is_no_answer(finished):
        if attempt < MAX_ATTEMPTS:
            retry_at = datetime.now(timezone.utc) + timedelta(seconds=RETRY_DELAY_SEC)
            store.update_contact(
                batch_id,
                contact.contact_id,
                status="waiting_retry",
                result="no_answer",
                result_summary=getattr(finished, "result_summary", "") or "未接通，将重试",
                fail_reason="",
                next_retry_at=retry_at.isoformat(),
                **rec_fields,
            )
            asyncio.create_task(
                _retry_later(batch_id, contact.contact_id, RETRY_DELAY_SEC),
                name=f"tmp-prod-retry-{contact.contact_id}-{attempt}",
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
                **rec_fields,
            )
    else:
        result = getattr(finished, "result", None) or "completed"
        err = getattr(finished, "error", "") or ""
        status = "failed" if getattr(finished, "status", "") == "failed" else "completed"
        store.update_contact(
            batch_id,
            contact.contact_id,
            status=status,
            result=result,
            result_summary=getattr(finished, "result_summary", "") or "",
            fail_reason=err if status == "failed" else "",
            next_retry_at="",
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
    # Fallback: any path's sibling stereo.wav
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

    Copies stereo (+ mono if present) so each contact has a stable path.
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
        # Best-effort: copy whatever exists in SIP record dir
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


async def _retry_later(batch_id: str, contact_id: str, delay_sec: int) -> None:
    try:
        await asyncio.sleep(delay_sec)
        store = get_batch_store()
        batch = store.get(batch_id)
        if not batch:
            return
        contact = next((c for c in batch.contacts if c.contact_id == contact_id), None)
        if not contact or contact.status != "waiting_retry":
            return
        await dial_one_contact(batch_id, contact, store=store, dry_run=False)
        _maybe_mark_batch_done(batch_id, store)
    except Exception:
        LOG.exception("tmp-prod retry failed batch=%s contact=%s", batch_id, contact_id)


def _maybe_mark_batch_done(batch_id: str, store: BatchStore) -> None:
    batch = store.get(batch_id)
    if not batch:
        return
    open_statuses = {"pending", "dialing", "in_call", "waiting_retry"}
    if any(c.status in open_statuses for c in batch.contacts):
        return
    batch.status = "done"
    store.save(batch)


async def run_batch(
    batch_id: str,
    *,
    dry_run: bool | None = None,
    stagger_sec: float = 0.5,
    max_concurrent: int | None = None,
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
        "tmp-prod batch start batch_id=%s contacts=%s max_concurrent=%s",
        batch_id,
        len(batch.contacts),
        limit,
    )

    async def _one(contact_id: str) -> None:
        async with sem:
            batch_now = store.get(batch_id)
            if not batch_now:
                return
            contact = next((c for c in batch_now.contacts if c.contact_id == contact_id), None)
            if not contact or contact.status in {"completed", "failed", "waiting_retry"}:
                return
            try:
                await dial_one_contact(batch_id, contact, store=store, dry_run=dry_run)
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
        if contact.status in {"completed", "failed", "waiting_retry"}:
            continue
        tasks.append(asyncio.create_task(_one(contact.contact_id), name=f"tmp-prod-{contact.contact_id}"))
        # Small launch stagger only (not wait-for-hangup); keeps SIP INVITE from thundering herd.
        if stagger_sec > 0 and i + 1 < len(batch.contacts):
            await asyncio.sleep(stagger_sec)

    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)

    _maybe_mark_batch_done(batch_id, store)
    out = store.get(batch_id)
    assert out
    return out
