# -*- coding: utf-8 -*-
"""In-memory IA-01 dispatch idempotency + task index."""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any


@dataclass
class AcceptedDispatch:
    dispatch_id: str
    task_id: int
    task_revision: int
    dispatch_type: str
    planned_call_at: str
    call_ids: list[str] = field(default_factory=list)
    request_snapshot: dict[str, Any] = field(default_factory=dict)
    terminal: bool = False


class DispatchStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._by_dispatch: dict[str, AcceptedDispatch] = {}
        # taskId → active INITIAL dispatchId (for TASK_ALREADY_DISPATCHED)
        self._active_initial: dict[int, str] = {}

    def get(self, dispatch_id: str) -> AcceptedDispatch | None:
        with self._lock:
            return self._by_dispatch.get(dispatch_id)

    def get_active_initial(self, task_id: int) -> str | None:
        with self._lock:
            return self._active_initial.get(task_id)

    def put(self, row: AcceptedDispatch) -> None:
        with self._lock:
            self._by_dispatch[row.dispatch_id] = row
            if row.dispatch_type == "INITIAL" and not row.terminal:
                self._active_initial[row.task_id] = row.dispatch_id

    def remove(self, dispatch_id: str) -> None:
        with self._lock:
            row = self._by_dispatch.pop(dispatch_id, None)
            if not row:
                return
            if (
                row.dispatch_type == "INITIAL"
                and self._active_initial.get(row.task_id) == dispatch_id
            ):
                self._active_initial.pop(row.task_id, None)

    def attach_call(self, dispatch_id: str, call_id: str) -> None:
        with self._lock:
            row = self._by_dispatch.get(dispatch_id)
            if row and call_id not in row.call_ids:
                row.call_ids.append(call_id)

    def mark_terminal(self, task_id: int) -> None:
        with self._lock:
            disp = self._active_initial.pop(task_id, None)
            if disp and disp in self._by_dispatch:
                self._by_dispatch[disp].terminal = True


_STORE: DispatchStore | None = None


def get_dispatch_store() -> DispatchStore:
    global _STORE
    if _STORE is None:
        _STORE = DispatchStore()
    return _STORE
