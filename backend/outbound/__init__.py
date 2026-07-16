# -*- coding: utf-8 -*-
"""Partner-facing outbound voice call API (催授权 + SIP 直连)."""

from backend.outbound.store import CallStore, get_call_store

__all__ = ["CallStore", "get_call_store"]
