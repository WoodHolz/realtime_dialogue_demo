# -*- coding: utf-8 -*-
"""IA-01 accept / dial orchestration."""
from __future__ import annotations

import logging
import random
import time

from backend.ia.mapping import (
    INTERNAL_SCENARIO,
    dispatch_to_outbound_payload,
    estimate_planned_call_at,
)
from backend.ia.models import TaskDispatchRequest, TaskDispatchResponse
from backend.ia.store import AcceptedDispatch, get_dispatch_store
from backend.outbound.service import create_outbound_call

LOG = logging.getLogger(__name__)


class DispatchError(Exception):
    def __init__(self, code: str, message: str, *, http_status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status


def _new_numeric_call_id() -> int:
    """Algorithm-generated callId (long) for IA-03A/03B/05."""
    return int(time.time() * 1000) * 1000 + random.randint(0, 999)


def _validate(req: TaskDispatchRequest) -> None:
    if not (req.dispatchId or "").strip():
        raise DispatchError("INVALID_DISPATCH", "dispatchId is required")
    if not (req.recipient.mobile or "").strip():
        raise DispatchError("INVALID_DISPATCH", "recipient.mobile is required")
    if not (req.recipient.name or "").strip():
        raise DispatchError("INVALID_DISPATCH", "recipient.name is required")
    if req.scriptId != "authReminder":
        raise DispatchError(
            "INVALID_DISPATCH",
            f"unsupported scriptId={req.scriptId!r}; smoke allows authReminder",
        )
    if req.identity.mode == "CLIENT_IDENTITY" and not (req.identity.customerBrandName or "").strip():
        raise DispatchError("INVALID_DISPATCH", "identity.customerBrandName required for CLIENT_IDENTITY")
    if req.identity.mode == "BRAND_SELF":
        if not (req.identity.ourBrandName or "").strip():
            raise DispatchError("INVALID_DISPATCH", "identity.ourBrandName required for BRAND_SELF")
        if not (req.identity.clientBuName or "").strip():
            raise DispatchError("INVALID_DISPATCH", "identity.clientBuName required for BRAND_SELF")
    if req.dispatchType == "PROMISE_FOLLOW_UP" and not req.context.recipientPromisedTime:
        raise DispatchError(
            "INVALID_DISPATCH",
            "context.recipientPromisedTime required for PROMISE_FOLLOW_UP",
        )


async def accept_task_dispatch(req: TaskDispatchRequest) -> TaskDispatchResponse:
    """Idempotent IA-01 accept; kicks first dial for non-duplicate requests."""
    _validate(req)
    store = get_dispatch_store()

    existing = store.get(req.dispatchId)
    if existing:
        return TaskDispatchResponse(
            accepted=True,
            duplicate=True,
            plannedCallAt=existing.planned_call_at,
            taskRevision=existing.task_revision,
        )

    if req.dispatchType == "INITIAL":
        active = store.get_active_initial(req.taskId)
        if active and active != req.dispatchId:
            raise DispatchError(
                "TASK_ALREADY_DISPATCHED",
                f"taskId={req.taskId} already has active INITIAL dispatchId={active}",
            )

    planned = estimate_planned_call_at(
        timezone_name=req.timezone or "Asia/Shanghai",
        call_window=req.callWindow,
        requested_at=req.requestedAt,
    )

    row = AcceptedDispatch(
        dispatch_id=req.dispatchId,
        task_id=req.taskId,
        task_revision=req.taskRevision,
        dispatch_type=req.dispatchType,
        planned_call_at=planned,
        request_snapshot=req.model_dump(mode="json"),
    )
    store.put(row)

    try:
        await _start_first_call(req, row)
    except DispatchError:
        _rollback_dispatch(req.dispatchId, req.taskId, req.dispatchType)
        raise
    except Exception as exc:
        _rollback_dispatch(req.dispatchId, req.taskId, req.dispatchType)
        LOG.exception("IA-01 dial failed dispatchId=%s", req.dispatchId)
        raise DispatchError(
            "TEMPORARILY_UNAVAILABLE",
            f"dial start failed: {exc}",
            http_status=503,
        ) from exc

    return TaskDispatchResponse(
        accepted=True,
        duplicate=False,
        plannedCallAt=planned,
        taskRevision=req.taskRevision,
    )


def _rollback_dispatch(dispatch_id: str, task_id: int, dispatch_type: str) -> None:
    del task_id, dispatch_type
    get_dispatch_store().remove(dispatch_id)


async def _start_first_call(req: TaskDispatchRequest, row: AcceptedDispatch) -> None:
    payload = dispatch_to_outbound_payload(req)
    numeric_call_id = _new_numeric_call_id()
    payload["ia"]["callId"] = numeric_call_id
    # Prefer not to auto-retry follow-up types via prod harness.
    payload["ia"]["allowAutoRetry"] = req.dispatchType == "INITIAL"

    task = await create_outbound_call(
        phone=req.recipient.mobile,
        scenario=INTERNAL_SCENARIO,
        external_id=req.externalOrderId,
        payload=payload,
        call_id=str(numeric_call_id),
    )
    get_dispatch_store().attach_call(req.dispatchId, task.call_id)
    LOG.info(
        "IA-01 dial started dispatchId=%s taskId=%s callId=%s type=%s",
        req.dispatchId,
        req.taskId,
        task.call_id,
        req.dispatchType,
    )
