# -*- coding: utf-8 -*-
"""容联 AI 外呼拨号规则：被叫前缀 a1、主叫 ANI、外显号池。"""
from __future__ import annotations

import re
from dataclasses import dataclass
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
    """Resolved numbers for one makecall."""

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


def normalize_phone(phone: str) -> str:
    """Strip spaces / dashes; keep leading + for international if present."""
    s = (phone or "").strip()
    s = re.sub(r"[\s\-\(\)]", "", s)
    return s


def apply_called_prefix(phone: str, prefix: str = DEFAULT_CALLED_PREFIX) -> str:
    """
    容联规则：被叫前缀 a1。
    已带前缀则不重复添加；已是完整 called 可原样返回。
    """
    raw = normalize_phone(phone)
    if not raw:
        return raw
    p = (prefix or "").strip()
    if not p:
        return raw
    # already prefixed (case-insensitive)
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
      - display: CONNECTOR caller = 外显（推荐，用户手机显示 021…）
      - ani: CONNECTOR caller = 主叫 01080435740
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
