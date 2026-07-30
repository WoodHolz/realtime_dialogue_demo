# -*- coding: utf-8 -*-
"""Analytics HTTP API: per-call + daily rollup + provider pull."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Header, HTTPException, Query

from backend.analytics.rollup import (
    build_range_summary,
    pull_and_merge_provider_day,
    rebuild_daily,
)
from backend.analytics.store import get_analytics_store, retention_days

router = APIRouter(prefix="/analytics/v1", tags=["analytics"])


def _auth(x_api_key: str | None) -> None:
    import os

    expected = (os.environ.get("ANALYTICS_API_KEY") or os.environ.get("OUTBOUND_API_KEY") or "").strip()
    if not expected:
        return
    if (x_api_key or "").strip() != expected:
        raise HTTPException(status_code=401, detail="unauthorized")


@router.get("/calls/{call_id}")
async def get_call_analytics(
    call_id: str,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict[str, Any]:
    _auth(x_api_key)
    rec = get_analytics_store().get(call_id)
    if not rec:
        raise HTTPException(status_code=404, detail="call analytics not found")
    return {"ok": True, "record": rec.to_dict()}


@router.get("/daily")
async def get_daily_analytics(
    day: str = Query(..., description="YYYY-MM-DD (Asia/Shanghai bucket)"),
    rebuild: bool = Query(False),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict[str, Any]:
    _auth(x_api_key)
    store = get_analytics_store()
    if rebuild or store.load_daily(day) is None:
        payload = rebuild_daily(day, store=store)
    else:
        payload = store.load_daily(day) or {}
    return {"ok": True, "daily": payload}


@router.get("/summary")
async def get_range_summary(
    days: int = Query(14, ge=1, le=90, description="Lookback days (capped by retention)"),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict[str, Any]:
    """Last N days rollup for /prod/single dashboard (default 14)."""
    _auth(x_api_key)
    capped = min(int(days), retention_days())
    return {"ok": True, "summary": build_range_summary(days=capped)}


@router.post("/purge")
async def purge_expired_analytics(
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict[str, Any]:
    _auth(x_api_key)
    result = get_analytics_store().purge_expired()
    return {"ok": True, "purge": result}


@router.post("/daily/{day}/pull-provider")
async def pull_provider_daily(
    day: str,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict[str, Any]:
    _auth(x_api_key)
    payload = await pull_and_merge_provider_day(day)
    return {"ok": True, "daily": payload}
