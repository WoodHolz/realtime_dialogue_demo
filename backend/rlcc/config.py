# -*- coding: utf-8 -*-
"""Load 容联云 RLCC settings from env and config.json."""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

from backend.rlcc.dial import (
    DEFAULT_ANI,
    DEFAULT_CALLED_PREFIX,
    DEFAULT_DISPLAY_POOL,
)


def _md5_lower(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest()


def _parse_csv_numbers(raw: str, fallback: Sequence[str] | None = None) -> tuple[str, ...]:
    parts = [p.strip() for p in (raw or "").replace(";", ",").split(",") if p.strip()]
    if parts:
        return tuple(parts)
    return tuple(fallback or DEFAULT_DISPLAY_POOL)


@dataclass(frozen=True)
class RlccConfig:
    enabled: bool = False
    api_base: str = ""
    url_3cs: str = ""
    company_id: str = ""
    abbreviate: str = ""
    connector_api: str = ""
    sdk_base_url: str = "https://cc.yuntongxun.com/ccc/public/cc_sdk"
    admin_account: str = ""
    admin_password_md5: str = ""
    connector_username: str = ""
    connector_password: str = ""
    operator_id: str = ""
    login_type: str = "2"
    softphone_type: str = "2"
    callback_base_url: str = ""
    forkstream_subscribe_url: str = ""
    forkstream_enabled: bool = False
    # AI 外呼拨号（容联号码池）
    called_prefix: str = DEFAULT_CALLED_PREFIX
    ani: str = DEFAULT_ANI
    display_numbers: tuple[str, ...] = field(default_factory=lambda: tuple(DEFAULT_DISPLAY_POOL))
    default_display: str = "02180639544"
    caller_mode: str = "display"

    def public_dict(self) -> dict:
        """Non-secret fields exposed to the browser."""
        return {
            "enabled": self.enabled,
            "url_3cs": self.url_3cs,
            "company_id": self.company_id,
            "abbreviate": self.abbreviate,
            "sdk_base_url": self.sdk_base_url,
            "operator_id": self.operator_id,
            "login_type": self.login_type,
            "softphone_type": self.softphone_type,
            "forkstream_enabled": self.forkstream_enabled,
            "configured": self.is_configured(),
            "called_prefix": self.called_prefix,
            "ani": self.ani,
            "default_display": self.default_display,
            "display_numbers": list(self.display_numbers),
            "caller_mode": self.caller_mode,
        }

    def is_configured(self) -> bool:
        """WebRTC 签入或 CONNECTOR 外呼任一就绪即可。"""
        if not self.enabled:
            return False
        has_connector = bool(
            self.connector_api and self.connector_username and self.connector_password
        )
        has_webrtc = bool(
            self.url_3cs and self.company_id and (self.connector_username or self.operator_id)
        )
        return has_connector or has_webrtc

    def connector_auth(self) -> tuple[str, str]:
        return self.connector_username, self.connector_password

    def config_token_body(self) -> dict | None:
        if not self.admin_account or not self.admin_password_md5:
            return None
        return {"account": self.admin_account, "pwd": self.admin_password_md5}


def load_rlcc_config() -> RlccConfig:
    root = Path(__file__).resolve().parent.parent.parent
    cfg: dict = {}
    try:
        cfg_path = root / "config.json"
        if cfg_path.exists():
            with cfg_path.open("r", encoding="utf-8") as f:
                data = json.load(f)
            cfg = data.get("rlcc", {}) or {}
    except Exception:
        cfg = {}

    def _env(name: str, fallback: str = "") -> str:
        return (os.environ.get(name, "") or "").strip() or str(cfg.get(name.lower(), fallback) or "").strip()

    def _env_bool(name: str, fallback: bool = False) -> bool:
        raw = os.environ.get(name)
        if raw is not None and str(raw).strip():
            return str(raw).strip().lower() in {"1", "true", "yes", "on"}
        if name.lower() in cfg:
            return bool(cfg.get(name.lower()))
        return fallback

    api_base = _env("RLCC_API_BASE")
    url_3cs = _env("RLCC_URL_3CS")
    if not url_3cs and api_base:
        url_3cs = api_base.rstrip("/") + "/3cs"

    admin_pwd = _env("RLCC_ADMIN_PASSWORD_MD5")
    if not admin_pwd:
        plain = _env("RLCC_ADMIN_PASSWORD")
        if plain:
            admin_pwd = _md5_lower(plain)

    operator_id = _env("RLCC_OPERATOR_ID")
    abbreviate = _env("RLCC_ABBREVIATE")
    connector_user = _env("RLCC_CONNECTOR_USERNAME")
    connector_pass = _env("RLCC_CONNECTOR_PASSWORD")
    if not connector_user and operator_id and abbreviate:
        connector_user = f"{operator_id}@{abbreviate}"

    callback_base = _env("RLCC_CALLBACK_BASE_URL")
    fork_subscribe = callback_base.rstrip("/") + "/api/rlcc/forkstream/on-call" if callback_base else ""

    pool_raw = _env("RLCC_DISPLAY_NUMBERS") or ",".join(
        str(x) for x in (cfg.get("display_numbers") or DEFAULT_DISPLAY_POOL)
    )
    display_pool = _parse_csv_numbers(pool_raw)
    default_display = _env("RLCC_DEFAULT_DISPLAY") or str(cfg.get("default_display") or "02180639544")
    ani = _env("RLCC_ANI") or str(cfg.get("ani") or DEFAULT_ANI)
    called_prefix = _env("RLCC_CALLED_PREFIX") or str(cfg.get("called_prefix") or DEFAULT_CALLED_PREFIX)
    caller_mode = _env("RLCC_CALLER_MODE") or str(cfg.get("caller_mode") or "display")

    return RlccConfig(
        enabled=_env_bool("RLCC_ENABLED", bool(cfg.get("enabled", False))),
        api_base=api_base,
        url_3cs=url_3cs,
        company_id=_env("RLCC_COMPANY_ID"),
        abbreviate=abbreviate,
        connector_api=_env("RLCC_CONNECTOR_API") or (api_base.rstrip("/") + "/api/v1/cti/cc/api" if api_base else ""),
        sdk_base_url=_env("RLCC_SDK_BASE_URL", str(cfg.get("sdk_base_url") or "https://cc.yuntongxun.com/ccc/public/cc_sdk")),
        admin_account=_env("RLCC_ADMIN_ACCOUNT"),
        admin_password_md5=admin_pwd,
        connector_username=connector_user,
        connector_password=connector_pass,
        operator_id=operator_id,
        login_type=_env("RLCC_LOGIN_TYPE", str(cfg.get("login_type") or "2")),
        softphone_type=_env("RLCC_SOFTPHONE_TYPE", str(cfg.get("softphone_type") or "2")),
        callback_base_url=callback_base,
        forkstream_subscribe_url=fork_subscribe,
        forkstream_enabled=_env_bool("RLCC_FORKSTREAM_ENABLED", bool(cfg.get("forkstream_enabled", False))),
        called_prefix=called_prefix,
        ani=ani,
        display_numbers=display_pool,
        default_display=default_display,
        caller_mode=caller_mode,
    )
