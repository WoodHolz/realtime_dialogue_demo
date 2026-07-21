# -*- coding: utf-8 -*-
"""Simple SIP concurrency + peer pool (容联多节点).

- Global concurrent dial/call cap (default 60)
- Round-robin across SIP_DST_HOSTS
"""
from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass

LOG = logging.getLogger(__name__)

DEFAULT_PEERS = (
    "124.70.154.200:5060",
    "60.204.136.226:5060",
    "121.43.183.130:5060",
    "121.43.188.8:5060",
)


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name, default) or default).strip()


def _env_int(name: str, default: int) -> int:
    raw = _env(name, "")
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def parse_peer(raw: str, default_port: int = 5060) -> tuple[str, int] | None:
    text = (raw or "").strip()
    if not text or text.startswith("#"):
        return None
    if ":" in text:
        host, _, port_s = text.rpartition(":")
        host = host.strip()
        if not host:
            return None
        try:
            return host, int(port_s.strip())
        except ValueError:
            return host, default_port
    return text, default_port


def load_sip_peers() -> list[tuple[str, int]]:
    """SIP_DST_HOSTS=ip:port,ip:port…；空则回退 SIP_DST_HOST 或内置 4 节点。"""
    default_port = _env_int("SIP_DST_PORT", 5060)
    raw_list = _env("SIP_DST_HOSTS", "")
    peers: list[tuple[str, int]] = []
    if raw_list:
        for part in raw_list.split(","):
            peer = parse_peer(part, default_port)
            if peer:
                peers.append(peer)
    if not peers:
        single = _env("SIP_DST_HOST", "")
        if single:
            peer = parse_peer(single, default_port)
            if peer:
                peers.append(peer)
    if not peers:
        for item in DEFAULT_PEERS:
            peer = parse_peer(item, default_port)
            if peer:
                peers.append(peer)
    # de-dupe preserve order
    seen: set[tuple[str, int]] = set()
    out: list[tuple[str, int]] = []
    for p in peers:
        if p not in seen:
            seen.add(p)
            out.append(p)
    return out


def max_concurrent() -> int:
    n = _env_int("OUTBOUND_MAX_CONCURRENT", 60)
    return max(1, n)


@dataclass
class TrafficSlot:
    host: str
    port: int
    active_total: int
    active_on_peer: int


class SipTrafficGate:
    """Process-local gate: concurrency cap + round-robin peer pick."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._rr = 0
        self._active_total = 0
        self._active_by_peer: dict[tuple[str, int], int] = {}

    def snapshot(self) -> dict[str, object]:
        peers = load_sip_peers()
        return {
            "max_concurrent": max_concurrent(),
            "active_total": self._active_total,
            "peers": [
                {
                    "host": h,
                    "port": p,
                    "active": self._active_by_peer.get((h, p), 0),
                }
                for h, p in peers
            ],
        }

    async def try_acquire(self, call_id: str = "") -> TrafficSlot | None:
        peers = load_sip_peers()
        if not peers:
            return None
        limit = max_concurrent()
        async with self._lock:
            if self._active_total >= limit:
                LOG.warning(
                    "SIP concurrency full active=%s max=%s call_id=%s",
                    self._active_total,
                    limit,
                    call_id or "-",
                )
                return None
            idx = self._rr % len(peers)
            self._rr += 1
            host, port = peers[idx]
            key = (host, port)
            self._active_total += 1
            self._active_by_peer[key] = self._active_by_peer.get(key, 0) + 1
            slot = TrafficSlot(
                host=host,
                port=port,
                active_total=self._active_total,
                active_on_peer=self._active_by_peer[key],
            )
            LOG.info(
                "SIP slot acquired call_id=%s peer=%s:%s active=%s/%s peer_active=%s",
                call_id or "-",
                host,
                port,
                self._active_total,
                limit,
                slot.active_on_peer,
            )
            return slot

    async def release(self, host: str, port: int, call_id: str = "") -> None:
        key = (host, port)
        async with self._lock:
            self._active_total = max(0, self._active_total - 1)
            cur = self._active_by_peer.get(key, 0)
            if cur <= 1:
                self._active_by_peer.pop(key, None)
            else:
                self._active_by_peer[key] = cur - 1
            LOG.info(
                "SIP slot released call_id=%s peer=%s:%s active=%s/%s",
                call_id or "-",
                host,
                port,
                self._active_total,
                max_concurrent(),
            )


_GATE: SipTrafficGate | None = None


def get_traffic_gate() -> SipTrafficGate:
    global _GATE
    if _GATE is None:
        _GATE = SipTrafficGate()
    return _GATE
