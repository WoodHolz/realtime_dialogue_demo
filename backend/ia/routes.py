# -*- coding: utf-8 -*-
"""IA-01 HTTP routes: platform → algorithm."""
from __future__ import annotations

import logging
import os

from fastapi import APIRouter, Header, HTTPException
from fastapi.responses import JSONResponse

from backend.ia.dispatch import DispatchError, accept_task_dispatch
from backend.ia.models import TaskDispatchError, TaskDispatchRequest, TaskDispatchResponse

LOG = logging.getLogger(__name__)

router = APIRouter(prefix="/va-algo/v1", tags=["ia-algo"])


def _verify_algo_api_key(provided: str | None) -> bool:
    """Optional auth; empty IA_ALGO_API_KEY → open (smoke)."""
    expected = (os.environ.get("IA_ALGO_API_KEY") or "").strip()
    if not expected:
        return True
    return (provided or "").strip() == expected


@router.post("/task-dispatch", response_model=TaskDispatchResponse)
async def task_dispatch(
    body: TaskDispatchRequest,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    authorization: str | None = Header(default=None),
) -> TaskDispatchResponse | JSONResponse:
    """IA-01: AI platform dispatches an outbound execution to the algorithm side."""
    token = None
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()
    if not _verify_algo_api_key(x_api_key or token):
        raise HTTPException(status_code=401, detail="Invalid or missing API key")

    try:
        return await accept_task_dispatch(body)
    except DispatchError as exc:
        LOG.warning("IA-01 reject code=%s msg=%s", exc.code, exc.message)
        return JSONResponse(
            status_code=exc.http_status,
            content=TaskDispatchError(errorCode=exc.code, message=exc.message).model_dump(),
        )
