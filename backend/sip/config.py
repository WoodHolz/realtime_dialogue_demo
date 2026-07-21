# -*- coding: utf-8 -*-
"""SIP / RTP runtime configuration (env-driven)."""
from __future__ import annotations

import os
from dataclasses import dataclass

from backend.outbound.dial import DEFAULT_ANI, DEFAULT_CALLED_PREFIX


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


def _env_float(name: str, default: float) -> float:
    raw = _env(name, "")
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = _env(name, "")
    if not raw:
        return default
    return raw.lower() in {"1", "true", "yes", "on"}


DEFAULT_OPENING_NUDGE = (
    "（电话已接通。请立即按系统角色中的【合规开场】说出标准开场白，不要等待用户先说话。）"
)


@dataclass(frozen=True)
class SipConfig:
    """Peer + local media endpoints for SIP direct calling."""

    dst_host: str = "124.70.154.200"
    dst_port: int = 5060
    # Public IP advertised in SDP / Contact (容联 media destination).
    src_ip: str = "1.13.182.189"
    # Local RTP bind (cloud EIP often cannot bind; use 0.0.0.0).
    bind_ip: str = "0.0.0.0"
    src_sip_port: int = 5070
    # Preferred RTP port; if busy, try the next rtp_port_count ports before INVITE.
    rtp_port: int = 10000
    rtp_port_count: int = 50
    from_user: str = DEFAULT_ANI
    called_prefix: str = DEFAULT_CALLED_PREFIX
    ring_timeout_sec: float = 45.0
    # Max media hold after answer when agent does not hang up earlier.
    max_call_sec: float = 600.0
    # SIP TCP double-CRLF keepalive interval (avoid NAT/LB idle drop ~10-30s).
    sip_keepalive_sec: float = 10.0
    # Optional recording directory; empty = disabled.
    record_dir: str = ""
    # After answer, inject opening_nudge via Duplex text query so AI speaks first.
    ai_speak_first: bool = True
    opening_nudge: str = DEFAULT_OPENING_NUDGE

    def is_configured(self) -> bool:
        return bool(self.dst_host and self.src_ip and self.from_user)


def load_sip_config() -> SipConfig:
    """Load SIP settings from env (SIP_*)."""
    ani = _env("SIP_FROM_USER") or DEFAULT_ANI
    if "#" in ani:
        ani = ani.split("#", 1)[0].strip()
    prefix = _env("SIP_CALLED_PREFIX") or DEFAULT_CALLED_PREFIX
    if "#" in prefix:
        prefix = prefix.split("#", 1)[0].strip()
    ai_speak_first = _env_bool("SIP_AI_SPEAK_FIRST", True)
    opening_nudge = _env("SIP_OPENING_NUDGE", DEFAULT_OPENING_NUDGE)
    if not ai_speak_first:
        opening_nudge = ""

    return SipConfig(
        dst_host=_env("SIP_DST_HOST", "124.70.154.200"),
        dst_port=_env_int("SIP_DST_PORT", 5060),
        src_ip=_env("SIP_SRC_IP", "1.13.182.189"),
        bind_ip=_env("SIP_BIND_IP", "0.0.0.0"),
        src_sip_port=_env_int("SIP_SRC_PORT", 5070),
        rtp_port=_env_int("SIP_RTP_PORT", 10000),
        rtp_port_count=_env_int("SIP_RTP_PORT_COUNT", 50),
        from_user=ani,
        called_prefix=prefix,
        ring_timeout_sec=_env_float("SIP_RING_TIMEOUT", 45.0),
        max_call_sec=_env_float("SIP_MAX_CALL_SEC", 600.0),
        sip_keepalive_sec=_env_float("SIP_KEEPALIVE_SEC", 10.0),
        record_dir=_env("SIP_RECORD_DIR", ""),
        ai_speak_first=ai_speak_first,
        opening_nudge=opening_nudge,
    )
