# -*- coding: utf-8 -*-
"""Broadcast demo ASR events to /ws/demo-asr viewer clients."""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Any

from fastapi import WebSocket

DEMO_ASR_EVENT_TYPES = frozenset({"asr", "asr_ended", "voice_input_started", "local_asr_text"})

_asr_viewers_lock = asyncio.Lock()
_asr_viewers: list[WebSocket] = []
_last_event: dict[str, Any] | None = None


def _stamp(payload: dict[str, Any]) -> dict[str, Any]:
    out = dict(payload)
    out.setdefault("at", datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z")
    return out


async def broadcast_demo_asr_event(payload: dict[str, Any]) -> None:
    global _last_event
    if payload.get("type") not in DEMO_ASR_EVENT_TYPES:
        return
    snap = _stamp(payload)
    _last_event = snap
    wire = json.dumps(snap, ensure_ascii=False)
    async with _asr_viewers_lock:
        targets = list(_asr_viewers)
    dead: list[WebSocket] = []
    for ws in targets:
        try:
            await ws.send_text(wire)
        except Exception:
            dead.append(ws)
    if dead:
        async with _asr_viewers_lock:
            for ws in dead:
                try:
                    _asr_viewers.remove(ws)
                except ValueError:
                    pass


def schedule_demo_asr_broadcast(payload: dict[str, Any]) -> None:
    try:
        asyncio.get_running_loop().create_task(broadcast_demo_asr_event(payload))
    except RuntimeError:
        pass


async def register_demo_asr_viewer(websocket: WebSocket) -> None:
    async with _asr_viewers_lock:
        _asr_viewers.append(websocket)


async def unregister_demo_asr_viewer(websocket: WebSocket) -> None:
    async with _asr_viewers_lock:
        try:
            _asr_viewers.remove(websocket)
        except ValueError:
            pass


def demo_asr_last_event() -> dict[str, Any] | None:
    return _last_event
