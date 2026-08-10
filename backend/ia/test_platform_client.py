# -*- coding: utf-8 -*-
from __future__ import annotations

import httpx

from backend.ia.platform_client import _platform_resp_summary


def test_d0004_empty_message_gets_hint():
    req = httpx.Request("POST", "https://example.test/report/voice/callOutcomes")
    resp = httpx.Response(
        500,
        request=req,
        json={"code": "D0004", "message": "", "timestamp": 1},
    )
    summary = _platform_resp_summary(resp)
    assert "D0004" in summary
    assert "平台暂不可用" in summary
    assert "TEMPORARILY_UNAVAILABLE" in summary
