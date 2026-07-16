# -*- coding: utf-8 -*-
"""General SIP UAC + RTP media for outbound telephony (scenario-agnostic)."""
from __future__ import annotations

from backend.sip.config import SipConfig, load_sip_config
from backend.sip.session import SipCallResult, SipCallSession

__all__ = [
    "SipConfig",
    "SipCallResult",
    "SipCallSession",
    "load_sip_config",
]
