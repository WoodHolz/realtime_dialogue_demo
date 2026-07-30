# -*- coding: utf-8 -*-
"""JSONL persistence for per-call analytics + daily rollup files."""
from __future__ import annotations

import json
import logging
import os
import threading
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from backend.analytics.models import CallAnalyticsRecord

LOG = logging.getLogger(__name__)
BJ = ZoneInfo("Asia/Shanghai")

# Keep call/daily analytics for this many Asia/Shanghai calendar days, then delete.
DEFAULT_RETENTION_DAYS = 14


def retention_days() -> int:
    raw = (os.environ.get("ANALYTICS_RETENTION_DAYS") or "").strip()
    if raw:
        try:
            return max(1, min(365, int(raw)))
        except ValueError:
            pass
    return DEFAULT_RETENTION_DAYS


def analytics_root() -> Path:
    env = (os.environ.get("ANALYTICS_DATA_DIR") or "").strip()
    if env:
        return Path(env).expanduser().resolve()
    return Path(__file__).resolve().parents[2] / "data" / "analytics"


def _day_key(iso_ts: str | None = None) -> str:
    """Bucket by Asia/Shanghai calendar day for ops-friendly daily stats."""
    if iso_ts:
        try:
            raw = iso_ts.replace("Z", "+00:00")
            dt = datetime.fromisoformat(raw)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(BJ).date().isoformat()
        except Exception:
            pass
    return datetime.now(BJ).date().isoformat()


class AnalyticsStore:
    def __init__(self, root: Path | None = None) -> None:
        self.root = root or analytics_root()
        self.calls_dir = self.root / "calls"
        self.daily_dir = self.root / "daily"
        self._lock = threading.Lock()
        self._by_id: dict[str, CallAnalyticsRecord] = {}
        self._component_pending: dict[str, dict[str, dict[str, Any]]] = {}
        self._last_purge_mono: float = 0.0

    def _ensure_dirs(self) -> None:
        self.calls_dir.mkdir(parents=True, exist_ok=True)
        self.daily_dir.mkdir(parents=True, exist_ok=True)

    def _calls_path(self, day: str) -> Path:
        return self.calls_dir / f"{day}.jsonl"

    def _daily_path(self, day: str) -> Path:
        return self.daily_dir / f"{day}.json"

    def put(self, record: CallAnalyticsRecord) -> CallAnalyticsRecord:
        """Append/overwrite call record in memory + JSONL; refresh day rollup."""
        pending = self._component_pending.pop(record.call_id, None)
        if pending:
            usage = dict(record.usage or {})
            components = dict(usage.get("components") or {})
            for name, metrics in pending.items():
                prev = dict(components.get(name) or {})
                for k, v in metrics.items():
                    if isinstance(v, (int, float)) and isinstance(prev.get(k), (int, float)):
                        prev[k] = float(prev[k]) + float(v)
                    else:
                        prev[k] = v
                components[name] = prev
            usage["components"] = components
            record.usage = usage

        with self._lock:
            self._ensure_dirs()
            self._by_id[record.call_id] = record
            day = _day_key(record.ended_at or record.started_at or record.recorded_at)
            path = self._calls_path(day)
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record.to_dict(), ensure_ascii=False) + "\n")

        try:
            from backend.analytics.rollup import rebuild_daily

            rebuild_daily(day, store=self)
        except Exception:
            LOG.exception("analytics daily rollup failed day=%s", day)
        self.maybe_purge_expired()
        return record

    def cutoff_day(self, *, keep_days: int | None = None) -> str:
        """Oldest day (inclusive) still retained, Asia/Shanghai."""
        keep = retention_days() if keep_days is None else max(1, int(keep_days))
        today = datetime.now(BJ).date()
        # keep=14 → today and previous 13 days → cutoff = today - 13
        from datetime import timedelta

        return (today - timedelta(days=keep - 1)).isoformat()

    def maybe_purge_expired(self, *, force: bool = False, min_interval_sec: float = 3600.0) -> dict[str, Any]:
        """Throttle disk purge; force=True bypasses interval (startup)."""
        import time

        now = time.monotonic()
        if not force and (now - self._last_purge_mono) < min_interval_sec:
            return {"skipped": True, "reason": "throttled"}
        return self.purge_expired()

    def purge_expired(self, *, keep_days: int | None = None) -> dict[str, Any]:
        """Delete calls/*.jsonl and daily/*.json older than retention window."""
        import time

        keep = retention_days() if keep_days is None else max(1, int(keep_days))
        cutoff = self.cutoff_day(keep_days=keep)
        removed_calls: list[str] = []
        removed_daily: list[str] = []
        with self._lock:
            self._ensure_dirs()
            for path in list(self.calls_dir.glob("*.jsonl")):
                day = path.stem
                if len(day) == 10 and day < cutoff:
                    try:
                        path.unlink(missing_ok=True)
                        removed_calls.append(day)
                    except OSError:
                        LOG.exception("failed removing expired calls file %s", path)
            for path in list(self.daily_dir.glob("*.json")):
                day = path.stem
                if len(day) == 10 and day < cutoff:
                    try:
                        path.unlink(missing_ok=True)
                        removed_daily.append(day)
                    except OSError:
                        LOG.exception("failed removing expired daily file %s", path)
            # Drop in-memory records outside retention.
            drop_ids = [
                cid
                for cid, rec in self._by_id.items()
                if _day_key(rec.ended_at or rec.started_at or rec.recorded_at) < cutoff
            ]
            for cid in drop_ids:
                self._by_id.pop(cid, None)
            self._last_purge_mono = time.monotonic()
        if removed_calls or removed_daily:
            LOG.info(
                "analytics purge keep_days=%s cutoff=%s removed_calls=%s removed_daily=%s",
                keep,
                cutoff,
                removed_calls,
                removed_daily,
            )
        return {
            "skipped": False,
            "keep_days": keep,
            "cutoff_day": cutoff,
            "removed_calls": removed_calls,
            "removed_daily": removed_daily,
        }

    def list_days(self, days: int = 14) -> list[str]:
        """Recent Asia/Shanghai day keys (oldest → newest), capped by retention."""
        from datetime import timedelta

        keep = min(max(1, int(days)), retention_days())
        today = datetime.now(BJ).date()
        return [
            (today - timedelta(days=offset)).isoformat()
            for offset in range(keep - 1, -1, -1)
        ]

    def get(self, call_id: str) -> CallAnalyticsRecord | None:
        with self._lock:
            hit = self._by_id.get(call_id)
            if hit:
                return hit
        # Scan recent days (today ± 2) then fall back to full scan of calls dir.
        today = datetime.now(BJ).date()
        days = [
            (today.toordinal() + off)
            for off in (0, -1, 1, -2, 2)
        ]
        for ordinal in days:
            d = date.fromordinal(ordinal).isoformat()
            rec = self._find_in_day_file(d, call_id)
            if rec:
                with self._lock:
                    self._by_id[call_id] = rec
                return rec
        with self._lock:
            self._ensure_dirs()
            for path in sorted(self.calls_dir.glob("*.jsonl"), reverse=True):
                rec = self._find_in_file(path, call_id)
                if rec:
                    self._by_id[call_id] = rec
                    return rec
        return None

    def _find_in_day_file(self, day: str, call_id: str) -> CallAnalyticsRecord | None:
        return self._find_in_file(self._calls_path(day), call_id)

    def _find_in_file(self, path: Path, call_id: str) -> CallAnalyticsRecord | None:
        if not path.is_file():
            return None
        last: CallAnalyticsRecord | None = None
        try:
            with path.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        data = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if data.get("call_id") == call_id:
                        last = CallAnalyticsRecord.from_dict(data)
        except OSError:
            return None
        return last

    def list_day(self, day: str) -> list[CallAnalyticsRecord]:
        """Latest record per call_id for a calendar day (BJ)."""
        path = self._calls_path(day)
        by_id: dict[str, CallAnalyticsRecord] = {}
        if path.is_file():
            try:
                with path.open("r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            data = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        rec = CallAnalyticsRecord.from_dict(data)
                        by_id[rec.call_id] = rec
            except OSError:
                LOG.exception("failed reading analytics day=%s", day)
        with self._lock:
            for rec in self._by_id.values():
                if _day_key(rec.ended_at or rec.started_at or rec.recorded_at) == day:
                    by_id[rec.call_id] = rec
        return sorted(by_id.values(), key=lambda r: r.ended_at or r.recorded_at)

    def merge_component(
        self,
        call_id: str,
        component: str,
        metrics: dict[str, Any],
    ) -> None:
        """Attach LLM/component usage to an existing call or hold until call is recorded."""
        if not call_id or not component:
            return
        with self._lock:
            rec = self._by_id.get(call_id)
            if rec is None:
                pending = self._component_pending.setdefault(call_id, {})
                prev = dict(pending.get(component) or {})
                for k, v in (metrics or {}).items():
                    if isinstance(v, (int, float)) and isinstance(prev.get(k), (int, float)):
                        prev[k] = float(prev[k]) + float(v)
                    else:
                        prev[k] = v
                pending[component] = prev
                return
            usage = dict(rec.usage or {})
            components = dict(usage.get("components") or {})
            prev = dict(components.get(component) or {})
            for k, v in (metrics or {}).items():
                if isinstance(v, (int, float)) and isinstance(prev.get(k), (int, float)):
                    prev[k] = float(prev[k]) + float(v)
                else:
                    prev[k] = v
            components[component] = prev
            usage["components"] = components
            rec.usage = usage
            # Append an updated line so disk stays eventual-consistent.
            day = _day_key(rec.ended_at or rec.started_at or rec.recorded_at)
            self._ensure_dirs()
            with self._calls_path(day).open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec.to_dict(), ensure_ascii=False) + "\n")

    def load_daily(self, day: str) -> dict[str, Any] | None:
        path = self._daily_path(day)
        if not path.is_file():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            LOG.exception("failed reading daily rollup day=%s", day)
            return None

    def save_daily(self, day: str, payload: dict[str, Any]) -> Path:
        with self._lock:
            self._ensure_dirs()
            path = self._daily_path(day)
            path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            return path


_STORE: AnalyticsStore | None = None


def get_analytics_store() -> AnalyticsStore:
    global _STORE
    if _STORE is None:
        _STORE = AnalyticsStore()
    return _STORE


def reset_analytics_store_for_tests(root: Path | None = None) -> AnalyticsStore:
    global _STORE
    _STORE = AnalyticsStore(root=root)
    return _STORE
