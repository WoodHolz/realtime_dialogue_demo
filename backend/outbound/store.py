# -*- coding: utf-8 -*-
"""In-memory call task store (demo). Replace with Redis/DB in production."""
from __future__ import annotations

import threading
from typing import Any

from backend.outbound.models import CallTask


class CallStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._by_id: dict[str, CallTask] = {}

    def put(self, task: CallTask) -> CallTask:
        with self._lock:
            self._by_id[task.call_id] = task
            return task

    def get(self, call_id: str) -> CallTask | None:
        with self._lock:
            return self._by_id.get(call_id)

    def update(self, call_id: str, **fields: Any) -> CallTask | None:
        with self._lock:
            task = self._by_id.get(call_id)
            if task is None:
                return None
            for k, v in fields.items():
                if hasattr(task, k):
                    setattr(task, k, v)
            task.touch()
            return task

    def list_recent(self, limit: int = 50) -> list[CallTask]:
        with self._lock:
            items = sorted(self._by_id.values(), key=lambda t: t.created_at, reverse=True)
            return items[: max(1, min(limit, 200))]


_STORE: CallStore | None = None


def get_call_store() -> CallStore:
    global _STORE
    if _STORE is None:
        _STORE = CallStore()
    return _STORE
