# -*- coding: utf-8 -*-
"""Daily rollup: avg duration, hangup histograms, local vs provider usage."""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from backend.analytics.models import CallAnalyticsRecord, ProviderDayUsage
from backend.analytics.store import AnalyticsStore, get_analytics_store

LOG = logging.getLogger(__name__)

_SUM_KEYS = (
    "input_tokens",
    "output_tokens",
    "total_tokens",
    "audio_in_sec",
    "audio_out_sec",
    "asr_chars",
    "tts_chars",
    "turns_user",
    "turns_assistant",
)


def _percentile(sorted_vals: list[float], p: float) -> float | None:
    if not sorted_vals:
        return None
    if len(sorted_vals) == 1:
        return float(sorted_vals[0])
    k = (len(sorted_vals) - 1) * p
    f = int(k)
    c = min(f + 1, len(sorted_vals) - 1)
    if f == c:
        return float(sorted_vals[f])
    return float(sorted_vals[f] + (sorted_vals[c] - sorted_vals[f]) * (k - f))


def _sum_usage_slice(records: list[CallAnalyticsRecord], key: str) -> dict[str, float]:
    totals: dict[str, float] = {k: 0.0 for k in _SUM_KEYS}
    for rec in records:
        slice_ = (rec.usage or {}).get(key) or {}
        if not isinstance(slice_, dict):
            continue
        for k in _SUM_KEYS:
            v = slice_.get(k)
            if isinstance(v, (int, float)):
                totals[k] += float(v)
        # Nested components under local? also under usage.components
    if key == "local":
        for rec in records:
            components = (rec.usage or {}).get("components") or {}
            if not isinstance(components, dict):
                continue
            for metrics in components.values():
                if not isinstance(metrics, dict):
                    continue
                for k in _SUM_KEYS:
                    v = metrics.get(k)
                    if isinstance(v, (int, float)):
                        totals[k] += float(v)
    return {k: round(v, 4) for k, v in totals.items() if v}


def _hist(values: list[str]) -> dict[str, int]:
    out: dict[str, int] = {}
    for v in values:
        key = (v or "").strip() or "unknown"
        out[key] = out.get(key, 0) + 1
    return dict(sorted(out.items(), key=lambda kv: (-kv[1], kv[0])))


def build_daily_rollup(
    day: str,
    records: list[CallAnalyticsRecord],
    *,
    provider_day: dict[str, Any] | None = None,
) -> dict[str, Any]:
    connected = [r for r in records if r.connected]
    durations = sorted(float(r.duration_sec or 0.0) for r in connected)
    avg = (sum(durations) / len(durations)) if durations else None
    return {
        "day": day,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "calls": len(records),
        "connected_calls": len(connected),
        "avg_duration_sec": round(avg, 3) if avg is not None else None,
        "p50_duration_sec": (
            round(_percentile(durations, 0.5) or 0.0, 3) if durations else None
        ),
        "p90_duration_sec": (
            round(_percentile(durations, 0.9) or 0.0, 3) if durations else None
        ),
        "total_duration_sec": round(sum(durations), 3) if durations else 0.0,
        "hangup_stage": _hist([r.hangup_stage for r in records]),
        "hangup_reason": _hist([r.hangup_reason for r in records]),
        "business_result": _hist([r.business_result for r in records if r.business_result]),
        "usage_local_totals": _sum_usage_slice(records, "local"),
        "usage_provider_session_totals": _sum_usage_slice(records, "provider_session"),
        "usage_provider_day": provider_day
        or {"status": "not_pulled", "providers": {}},
    }


def rebuild_daily(
    day: str,
    *,
    store: AnalyticsStore | None = None,
    keep_provider_pull: bool = True,
) -> dict[str, Any]:
    store = store or get_analytics_store()
    records = store.list_day(day)
    provider_day = None
    if keep_provider_pull:
        prev = store.load_daily(day) or {}
        provider_day = prev.get("usage_provider_day")
    payload = build_daily_rollup(day, records, provider_day=provider_day)
    store.save_daily(day, payload)
    return payload


async def pull_and_merge_provider_day(
    day: str,
    *,
    store: AnalyticsStore | None = None,
    provider_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Pull official day usage from configured adapters and merge into daily JSON."""
    from backend.analytics.providers import get_adapters

    store = store or get_analytics_store()
    adapters = get_adapters()
    if provider_ids:
        want = set(provider_ids)
        adapters = [a for a in adapters if a.provider_id in want]

    providers: dict[str, Any] = {}
    for adapter in adapters:
        try:
            result: ProviderDayUsage = await adapter.pull_day_usage(day)
        except Exception as exc:
            LOG.exception("provider day pull failed provider=%s day=%s", adapter.provider_id, day)
            result = ProviderDayUsage(
                provider_id=adapter.provider_id,
                day=day,
                status="error",
                error=str(exc),
            )
        providers[adapter.provider_id] = result.to_dict()

    records = store.list_day(day)
    provider_day = {
        "status": "ok" if providers else "skipped",
        "pulled_at": datetime.now(timezone.utc).isoformat(),
        "providers": providers,
    }
    if providers and all(p.get("status") == "skipped" for p in providers.values()):
        provider_day["status"] = "skipped"
    elif providers and any(p.get("status") == "error" for p in providers.values()):
        provider_day["status"] = "partial_error"

    payload = build_daily_rollup(day, records, provider_day=provider_day)
    store.save_daily(day, payload)
    return payload


def _merge_hist(dst: dict[str, int], src: dict[str, Any] | None) -> None:
    if not isinstance(src, dict):
        return
    for k, v in src.items():
        try:
            n = int(v)
        except (TypeError, ValueError):
            continue
        key = str(k) if k else "unknown"
        dst[key] = dst.get(key, 0) + n


def _merge_totals(dst: dict[str, float], src: dict[str, Any] | None) -> None:
    if not isinstance(src, dict):
        return
    for k, v in src.items():
        if isinstance(v, (int, float)):
            dst[k] = round(float(dst.get(k, 0.0)) + float(v), 4)


def build_range_summary(
    *,
    days: int = 14,
    store: AnalyticsStore | None = None,
) -> dict[str, Any]:
    """Aggregate retained daily rollups for the UI (default last 14 BJ days)."""
    store = store or get_analytics_store()
    store.maybe_purge_expired()
    day_keys = store.list_days(days)
    series: list[dict[str, Any]] = []
    hangup_stage: dict[str, int] = {}
    hangup_reason: dict[str, int] = {}
    business_result: dict[str, int] = {}
    usage_local: dict[str, float] = {}
    usage_provider_session: dict[str, float] = {}
    total_calls = 0
    total_connected = 0
    duration_sum = 0.0
    recent_calls: list[dict[str, Any]] = []

    for day in day_keys:
        daily = store.load_daily(day)
        if daily is None:
            daily = rebuild_daily(day, store=store)
        calls = int(daily.get("calls") or 0)
        connected = int(daily.get("connected_calls") or 0)
        avg = daily.get("avg_duration_sec")
        total_dur = float(daily.get("total_duration_sec") or 0.0)
        total_calls += calls
        total_connected += connected
        duration_sum += total_dur
        _merge_hist(hangup_stage, daily.get("hangup_stage"))
        _merge_hist(hangup_reason, daily.get("hangup_reason"))
        _merge_hist(business_result, daily.get("business_result"))
        _merge_totals(usage_local, daily.get("usage_local_totals"))
        _merge_totals(usage_provider_session, daily.get("usage_provider_session_totals"))
        series.append(
            {
                "day": day,
                "calls": calls,
                "connected_calls": connected,
                "avg_duration_sec": avg,
                "total_duration_sec": total_dur,
            }
        )
        # Latest few call records for the table (from this day).
        for rec in reversed(store.list_day(day)):
            recent_calls.append(
                {
                    "call_id": rec.call_id,
                    "day": day,
                    "duration_sec": rec.duration_sec,
                    "connected": rec.connected,
                    "hangup_stage": rec.hangup_stage,
                    "hangup_reason": rec.hangup_reason,
                    "business_result": rec.business_result,
                    "usage_local": (rec.usage or {}).get("local") or {},
                    "usage_provider_session": (rec.usage or {}).get("provider_session") or {},
                    "ended_at": rec.ended_at,
                }
            )

    recent_calls = recent_calls[:30]
    avg_all = (duration_sum / total_connected) if total_connected else None
    from backend.analytics.store import retention_days

    return {
        "days": len(day_keys),
        "retention_days": retention_days(),
        "from_day": day_keys[0] if day_keys else None,
        "to_day": day_keys[-1] if day_keys else None,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "calls": total_calls,
        "connected_calls": total_connected,
        "avg_duration_sec": round(avg_all, 3) if avg_all is not None else None,
        "total_duration_sec": round(duration_sum, 3),
        "hangup_stage": dict(sorted(hangup_stage.items(), key=lambda kv: (-kv[1], kv[0]))),
        "hangup_reason": dict(sorted(hangup_reason.items(), key=lambda kv: (-kv[1], kv[0]))),
        "business_result": dict(sorted(business_result.items(), key=lambda kv: (-kv[1], kv[0]))),
        "usage_local_totals": usage_local,
        "usage_provider_session_totals": usage_provider_session,
        "series": series,
        "recent_calls": recent_calls,
    }