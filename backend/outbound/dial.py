# -*- coding: utf-8 -*-
"""容联直连外呼拨号规则：被叫前缀 a1、主叫 ANI、外显号池（与 SIP 媒体无关）。"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence


DEFAULT_DISPLAY_POOL: tuple[str, ...] = (
    "02180639544",
    "02180639545",
    "02180639546",
    "02180639547",
    "02180639548",
    "02160602083",
    "02160602066",
    "02160602067",
    "02160602068",
    "02160602069",
    "02160602112",
)

DEFAULT_ANI = "01080435740"
DEFAULT_CALLED_PREFIX = "a1"


@dataclass(frozen=True)
class DialPlan:
    """Resolved numbers for one outbound dial."""

    phone_raw: str
    called: str
    ani: str
    display: str
    caller_for_api: str
    called_prefix: str

    def as_dict(self) -> dict[str, str]:
        return {
            "phone_raw": self.phone_raw,
            "called": self.called,
            "ani": self.ani,
            "display": self.display,
            "caller": self.caller_for_api,
            "called_prefix": self.called_prefix,
        }


@dataclass(frozen=True)
class DialSettings:
    called_prefix: str = DEFAULT_CALLED_PREFIX
    ani: str = DEFAULT_ANI
    default_display: str = ""
    display_numbers: tuple[str, ...] = DEFAULT_DISPLAY_POOL
    caller_mode: str = "display"


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name, default) or default).strip()


def _load_outbound_from_config() -> dict:
    try:
        root = Path(__file__).resolve().parent.parent.parent
        cfg_path = root / "config.json"
        if not cfg_path.exists():
            return {}
        with cfg_path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        outbound = data.get("outbound") or {}
        return outbound if isinstance(outbound, dict) else {}
    except Exception:
        return {}


def load_dial_settings() -> DialSettings:
    """Priority: env SIP_* → config.json outbound → defaults."""
    file_cfg = _load_outbound_from_config()
    ani = _env("SIP_FROM_USER") or _env("RLCC_ANI") or DEFAULT_ANI
    if "#" in ani:
        ani = ani.split("#", 1)[0].strip()
    prefix = _env("SIP_CALLED_PREFIX") or _env("RLCC_CALLED_PREFIX") or DEFAULT_CALLED_PREFIX
    if "#" in prefix:
        prefix = prefix.split("#", 1)[0].strip()

    default_display = (
        _env("SIP_DEFAULT_DISPLAY")
        or _env("RLCC_DEFAULT_DISPLAY")
        or str(file_cfg.get("default_display") or "")
    )
    pool_raw = _env("SIP_DISPLAY_NUMBERS") or _env("RLCC_DISPLAY_NUMBERS")
    if pool_raw:
        pool = tuple(x.strip() for x in pool_raw.split(",") if x.strip())
    else:
        cfg_pool = file_cfg.get("display_numbers")
        if isinstance(cfg_pool, list) and cfg_pool:
            pool = tuple(str(x).strip() for x in cfg_pool if str(x).strip())
        else:
            pool = DEFAULT_DISPLAY_POOL
    mode = _env("SIP_CALLER_MODE") or _env("RLCC_CALLER_MODE") or "display"
    return DialSettings(
        called_prefix=prefix,
        ani=ani,
        default_display=default_display or (pool[0] if pool else ""),
        display_numbers=pool,
        caller_mode=mode,
    )


def normalize_phone(phone: str) -> str:
    """Strip spaces / dashes; keep leading + for international if present."""
    s = (phone or "").strip()
    s = re.sub(r"[\s\-\(\)]", "", s)
    return s


def apply_called_prefix(phone: str, prefix: str = DEFAULT_CALLED_PREFIX) -> str:
    """容联规则：被叫前缀 a1。已带前缀则不重复添加。"""
    raw = normalize_phone(phone)
    if not raw:
        return raw
    p = (prefix or "").strip()
    if not p:
        return raw
    if raw.lower().startswith(p.lower()):
        return raw
    return f"{p}{raw}"


def resolve_display(
    display: str = "",
    *,
    default_display: str = "",
    pool: Sequence[str] | None = None,
) -> str:
    pool_list = [normalize_phone(x) for x in (pool or DEFAULT_DISPLAY_POOL) if str(x).strip()]
    chosen = normalize_phone(display) or normalize_phone(default_display)
    if chosen:
        return chosen
    return pool_list[0] if pool_list else ""


def build_dial_plan(
    phone: str,
    *,
    called_prefix: str = DEFAULT_CALLED_PREFIX,
    ani: str = DEFAULT_ANI,
    display: str = "",
    default_display: str = "",
    display_pool: Sequence[str] | None = None,
    caller_mode: str = "display",
) -> DialPlan:
    """
    caller_mode:
      - display: caller identity = 外显（用户手机显示 021…）
      - ani: caller identity = 主叫 01080435740
    """
    phone_raw = normalize_phone(phone)
    called = apply_called_prefix(phone_raw, called_prefix)
    ani_n = normalize_phone(ani) or DEFAULT_ANI
    disp = resolve_display(display, default_display=default_display, pool=display_pool)
    mode = (caller_mode or "display").strip().lower()
    caller = ani_n if mode == "ani" else (disp or ani_n)
    return DialPlan(
        phone_raw=phone_raw,
        called=called,
        ani=ani_n,
        display=disp or ani_n,
        caller_for_api=caller,
        called_prefix=(called_prefix or DEFAULT_CALLED_PREFIX).strip(),
    )
