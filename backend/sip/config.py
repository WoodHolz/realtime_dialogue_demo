# -*- coding: utf-8 -*-
"""SIP / RTP runtime configuration (env-driven, dial-plan numbers from RLCC helpers)."""
from __future__ import annotations

import os
from dataclasses import dataclass

from backend.rlcc.dial import DEFAULT_ANI, DEFAULT_CALLED_PREFIX


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
    rtp_port: int = 10000
    from_user: str = DEFAULT_ANI
    called_prefix: str = DEFAULT_CALLED_PREFIX
    ring_timeout_sec: float = 45.0
    # Max media hold after answer when agent does not hang up earlier.
    max_call_sec: float = 600.0
    # Optional recording directory; empty = disabled.
    record_dir: str = ""

    def is_configured(self) -> bool:
        return bool(self.dst_host and self.src_ip and self.from_user)


def load_sip_config() -> SipConfig:
    """Load SIP settings; fall back to RLCC_ANI / RLCC_CALLED_PREFIX when set."""
    ani = _env("SIP_FROM_USER") or _env("RLCC_ANI") or DEFAULT_ANI
    prefix = _env("SIP_CALLED_PREFIX") or _env("RLCC_CALLED_PREFIX") or DEFAULT_CALLED_PREFIX
    return SipConfig(
        dst_host=_env("SIP_DST_HOST", "124.70.154.200"),
        dst_port=_env_int("SIP_DST_PORT", 5060),
        src_ip=_env("SIP_SRC_IP", "1.13.182.189"),
        bind_ip=_env("SIP_BIND_IP", "0.0.0.0"),
        src_sip_port=_env_int("SIP_SRC_PORT", 5070),
        rtp_port=_env_int("SIP_RTP_PORT", 10000),
        from_user=ani,
        called_prefix=prefix,
        ring_timeout_sec=_env_float("SIP_RING_TIMEOUT", 45.0),
        max_call_sec=_env_float("SIP_MAX_CALL_SEC", 600.0),
        record_dir=_env("SIP_RECORD_DIR", ""),
    )
