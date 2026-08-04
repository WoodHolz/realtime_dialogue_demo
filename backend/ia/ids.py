# -*- coding: utf-8 -*-
"""Snowflake-style long IDs for IA callId (must not mimic platform taskId)."""
from __future__ import annotations

import os
import threading
import time


# Twitter-style: 41b time | 5b datacenter | 5b worker | 12b sequence
_EPOCH_MS = 1_704_067_200_000  # 2024-01-01 UTC
_DATACENTER_BITS = 5
_WORKER_BITS = 5
_SEQUENCE_BITS = 12
_MAX_SEQUENCE = (1 << _SEQUENCE_BITS) - 1
_WORKER_SHIFT = _SEQUENCE_BITS
_DATACENTER_SHIFT = _SEQUENCE_BITS + _WORKER_BITS
_TIMESTAMP_SHIFT = _SEQUENCE_BITS + _WORKER_BITS + _DATACENTER_BITS


class _Snowflake:
    def __init__(self, *, datacenter_id: int = 1, worker_id: int = 1) -> None:
        self._datacenter_id = datacenter_id & ((1 << _DATACENTER_BITS) - 1)
        self._worker_id = worker_id & ((1 << _WORKER_BITS) - 1)
        self._sequence = 0
        self._last_ms = -1
        self._lock = threading.Lock()

    def next_id(self) -> int:
        with self._lock:
            now = int(time.time() * 1000)
            if now < self._last_ms:
                # Clock skew: wait until last_ms
                now = self._last_ms
            if now == self._last_ms:
                self._sequence = (self._sequence + 1) & _MAX_SEQUENCE
                if self._sequence == 0:
                    while now <= self._last_ms:
                        now = int(time.time() * 1000)
            else:
                self._sequence = 0
            self._last_ms = now
            return (
                ((now - _EPOCH_MS) << _TIMESTAMP_SHIFT)
                | (self._datacenter_id << _DATACENTER_SHIFT)
                | (self._worker_id << _WORKER_SHIFT)
                | self._sequence
            )


def _env_int(name: str, default: int) -> int:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


_GENERATOR = _Snowflake(
    datacenter_id=_env_int("IA_SNOWFLAKE_DATACENTER_ID", 1),
    worker_id=_env_int("IA_SNOWFLAKE_WORKER_ID", 1),
)


def new_call_id() -> int:
    """Allocate a snowflake long for IA-03A/03B/05 callId."""
    return _GENERATOR.next_id()
