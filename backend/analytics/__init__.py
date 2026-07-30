# -*- coding: utf-8 -*-
"""Model-agnostic call analytics: usage (local + provider) and hangup funnel."""
from __future__ import annotations

from backend.analytics.meter import record_call_analytics, record_component_usage
from backend.analytics.models import CallAnalyticsRecord
from backend.analytics.store import get_analytics_store

__all__ = [
    "CallAnalyticsRecord",
    "get_analytics_store",
    "record_call_analytics",
    "record_component_usage",
]
