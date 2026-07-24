# -*- coding: utf-8 -*-
"""Lightweight due-at poller: claim pending actions and start fresh dials."""
from __future__ import annotations

import asyncio
import logging
import os

from backend.tmp_prod.harness.models import (
    ACTION_CALLBACK_RESCHEDULE,
    CALL_KIND_RESCHEDULE,
    STATUS_DONE,
    STATUS_SKIPPED,
)
from backend.tmp_prod.harness.store import get_action_store

LOG = logging.getLogger(__name__)

POLL_SEC = float(os.environ.get("TMP_PROD_HARNESS_POLL_SEC", "5"))

_scheduler_task: asyncio.Task | None = None


async def execute_action(action_id: str) -> None:
    """Run one claimed action: new outbound dial (short-lived task)."""
    from backend.tmp_prod.service import dial_one_contact, _maybe_mark_batch_done
    from backend.tmp_prod.store import get_batch_store

    store = get_action_store()
    action = store.get(action_id)
    if not action:
        return
    batch_store = get_batch_store()
    batch = batch_store.get(action.batch_id)
    if not batch:
        store.mark(action_id, status=STATUS_SKIPPED, error="batch missing")
        return
    contact = next((c for c in batch.contacts if c.contact_id == action.contact_id), None)
    if not contact:
        store.mark(action_id, status=STATUS_SKIPPED, error="contact missing")
        return

    expected = (
        "waiting_callback"
        if action.type == ACTION_CALLBACK_RESCHEDULE
        else "waiting_retry"
    )
    if contact.status not in {expected, "waiting_retry", "waiting_callback"}:
        store.mark(
            action_id,
            status=STATUS_SKIPPED,
            error=f"contact status={contact.status} not waiting",
        )
        return

    opts = dict(action.dial_options or {})
    callback_context = dict(action.callback_context or {})
    call_kind = ""
    if action.type == ACTION_CALLBACK_RESCHEDULE:
        call_kind = CALL_KIND_RESCHEDULE
        if not callback_context.get("call_kind"):
            callback_context["call_kind"] = CALL_KIND_RESCHEDULE

    try:
        await dial_one_contact(
            action.batch_id,
            contact,
            store=batch_store,
            identity_mode=str(opts.get("identity_mode") or "BRAND_SELF"),
            client_name=str(opts.get("client_name") or "比赛日科技"),
            our_brand_name=str(opts.get("our_brand_name") or "芝麻背调"),
            channel_brand_name=str(opts.get("channel_brand_name") or ""),
            voice=str(opts.get("voice") or ""),
            call_kind=call_kind,
            callback_context=callback_context or None,
        )
        store.mark(action_id, status=STATUS_DONE)
        _maybe_mark_batch_done(action.batch_id, batch_store)
    except Exception as exc:
        LOG.exception(
            "tmp-prod harness execute failed action=%s batch=%s contact=%s",
            action_id,
            action.batch_id,
            action.contact_id,
        )
        store.mark(action_id, status=STATUS_SKIPPED, error=str(exc))
        _maybe_mark_batch_done(action.batch_id, batch_store)


async def tick() -> int:
    """Claim due actions and spawn execute tasks. Returns number claimed."""
    claimed = get_action_store().claim_due()
    for action in claimed:
        asyncio.create_task(
            execute_action(action.action_id),
            name=f"tmp-prod-harness-{action.action_id}",
        )
    return len(claimed)


async def run_scheduler_loop(*, poll_sec: float | None = None) -> None:
    interval = POLL_SEC if poll_sec is None else float(poll_sec)
    LOG.info("tmp-prod harness scheduler started poll_sec=%s", interval)
    while True:
        try:
            n = await tick()
            if n:
                LOG.info("tmp-prod harness claimed %s due action(s)", n)
        except asyncio.CancelledError:
            raise
        except Exception:
            LOG.exception("tmp-prod harness scheduler tick failed")
        await asyncio.sleep(interval)


def start_harness_scheduler() -> asyncio.Task | None:
    """Idempotent: start background poller in the running event loop."""
    global _scheduler_task
    if _scheduler_task and not _scheduler_task.done():
        return _scheduler_task
    n = get_action_store().reset_running_to_pending()
    if n:
        LOG.info("tmp-prod harness reclaimed %s running action(s) → pending", n)
    _scheduler_task = asyncio.create_task(
        run_scheduler_loop(),
        name="tmp-prod-harness-scheduler",
    )
    return _scheduler_task


async def stop_harness_scheduler() -> None:
    global _scheduler_task
    task = _scheduler_task
    _scheduler_task = None
    if not task:
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    LOG.info("tmp-prod harness scheduler stopped")
