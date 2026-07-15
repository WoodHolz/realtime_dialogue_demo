# -*- coding: utf-8 -*-
"""HTTP client for 容联云 RLCC CONFIG / CONNECTOR APIs."""
from __future__ import annotations

import base64
import logging
import time
from typing import Any

import httpx

from backend.rlcc.config import RlccConfig

LOG = logging.getLogger(__name__)


class RlccClient:
    def __init__(self, config: RlccConfig) -> None:
        self._config = config
        self._token: str = ""
        self._token_expires_at: float = 0.0

    @property
    def config(self) -> RlccConfig:
        return self._config

    def _basic_auth_header(self) -> str:
        user, pwd = self._config.connector_auth()
        raw = f"{user}:{pwd}".encode("utf-8")
        return "Basic " + base64.b64encode(raw).decode("ascii")

    async def _get_config_token(self, force: bool = False) -> str:
        if not force and self._token and time.time() < self._token_expires_at:
            return self._token
        body = self._config.config_token_body()
        if not body:
            raise RuntimeError("RLCC admin credentials not configured (RLCC_ADMIN_ACCOUNT + RLCC_ADMIN_PASSWORD)")
        base = self._config.api_base.rstrip("/")
        url = f"{base}/api/v1/config/openapi/token/getToken"
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.post(url, json=body, headers={"Content-Type": "application/json"})
            resp.raise_for_status()
            data = resp.json()
        if data.get("code") not in (0, "0", None) and data.get("msg") != "success":
            raise RuntimeError(f"RLCC token error: {data}")
        token = ((data.get("data") or {}) or {}).get("token") or ""
        if not token:
            raise RuntimeError(f"RLCC token missing in response: {data}")
        self._token = token
        self._token_expires_at = time.time() + 3500
        return token

    async def connector_command(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not self._config.connector_api:
            raise RuntimeError("RLCC_CONNECTOR_API not configured")
        headers = {
            "Content-Type": "application/json",
            "Authorization": self._basic_auth_header(),
        }
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(self._config.connector_api, json=payload, headers=headers)
            resp.raise_for_status()
            return resp.json()

    async def make_call(
        self,
        called: str,
        *,
        caller: str = "",
        user_data: str = "",
        callback_url: str = "",
        agent: str = "",
        agentcaller: str = "",
        apply_dial_plan: bool = True,
        display: str = "",
    ) -> dict[str, Any]:
        """
        CONNECTOR makecall.
        apply_dial_plan=True 时自动：被叫加 a1 前缀、caller 填外显/主叫（按 caller_mode）。
        """
        from backend.rlcc.dial import build_dial_plan

        dial = None
        if apply_dial_plan:
            dial = build_dial_plan(
                called,
                called_prefix=self._config.called_prefix,
                ani=self._config.ani,
                display=display or caller,
                default_display=self._config.default_display,
                display_pool=self._config.display_numbers,
                caller_mode=self._config.caller_mode,
            )
            called = dial.called
            caller = dial.caller_for_api
            if not agentcaller:
                agentcaller = dial.ani

        payload: dict[str, Any] = {
            "cmd": "makecall",
            "called": called,
        }
        if caller:
            payload["caller"] = caller
        if agentcaller:
            payload["agentcaller"] = agentcaller
        if user_data:
            payload["userData"] = user_data
        if callback_url:
            payload["callBackUrl"] = callback_url
        elif self._config.callback_base_url:
            payload["callBackUrl"] = self._config.callback_base_url.rstrip("/") + "/api/rlcc/webhook/call-events"
        if agent:
            payload["agent"] = agent
        result = await self.connector_command(payload)
        if dial is not None and isinstance(result, dict):
            result = {**result, "_dial": dial.as_dict()}
        return result

    async def agent_logon(self, *, work_type: int = 1, online: int = 1, callback_url: str = "", agent: str = "") -> dict[str, Any]:
        payload: dict[str, Any] = {
            "cmd": "logon",
            "workType": work_type,
            "online": online,
        }
        if callback_url:
            payload["callBackUrl"] = callback_url
        elif self._config.callback_base_url:
            payload["callBackUrl"] = self._config.callback_base_url.rstrip("/") + "/api/rlcc/webhook/call-events"
        if agent:
            payload["agent"] = agent
        return await self.connector_command(payload)

    async def agent_logout(self, *, callback_url: str = "", agent: str = "") -> dict[str, Any]:
        payload: dict[str, Any] = {"cmd": "logout"}
        if callback_url:
            payload["callBackUrl"] = callback_url
        if agent:
            payload["agent"] = agent
        return await self.connector_command(payload)

    async def agent_idle(self, *, callback_url: str = "", agent: str = "") -> dict[str, Any]:
        payload: dict[str, Any] = {"cmd": "agentidle"}
        if callback_url:
            payload["callBackUrl"] = callback_url
        if agent:
            payload["agent"] = agent
        return await self.connector_command(payload)

    async def hangup(self, *, callback_url: str = "", agent: str = "") -> dict[str, Any]:
        payload: dict[str, Any] = {"cmd": "hangup"}
        if callback_url:
            payload["callBackUrl"] = callback_url
        if agent:
            payload["agent"] = agent
        return await self.connector_command(payload)

    async def subscribe_fork_stream(
        self,
        operators: str,
        *,
        abbreviate: str = "",
        subscribe_url: str = "",
    ) -> dict[str, Any]:
        token = await self._get_config_token()
        base = self._config.api_base.rstrip("/")
        url = f"{base}/api/v1/config/forkstream/subscribe"
        body = [
            {
                "operators": operators,
                "abbreviate": abbreviate or self._config.abbreviate,
                "subscribeUrl": subscribe_url or self._config.forkstream_subscribe_url,
            }
        ]
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.post(
                url,
                json=body,
                headers={"Content-Type": "application/json", "token": token},
            )
            resp.raise_for_status()
            return resp.json()

    async def unsubscribe_fork_stream(self, operators: str, *, abbreviate: str = "") -> dict[str, Any]:
        token = await self._get_config_token()
        base = self._config.api_base.rstrip("/")
        url = f"{base}/api/v1/config/forkstream/unsubscribe"
        body = [
            {
                "operators": operators,
                "abbreviate": abbreviate or self._config.abbreviate,
            }
        ]
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.post(
                url,
                json=body,
                headers={"Content-Type": "application/json", "token": token},
            )
            resp.raise_for_status()
            return resp.json()
