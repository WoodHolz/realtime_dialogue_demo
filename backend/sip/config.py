# -*- coding: utf-8 -*-
"""SIP / RTP runtime configuration (env + config.json outbound.sip)."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

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


def _load_outbound_sip_file() -> dict[str, Any]:
    """Read config.json → outbound.sip (optional)."""
    try:
        root = Path(__file__).resolve().parents[2]
        data = json.loads((root / "config.json").read_text(encoding="utf-8"))
        outbound = data.get("outbound") or {}
        sip = outbound.get("sip") if isinstance(outbound, dict) else None
        return dict(sip) if isinstance(sip, dict) else {}
    except Exception:
        return {}


def _file_bool(file_cfg: dict[str, Any], key: str, default: bool) -> bool:
    if key not in file_cfg:
        return default
    val = file_cfg.get(key)
    if isinstance(val, bool):
        return val
    if isinstance(val, (int, float)):
        return val != 0
    text = str(val or "").strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return default


def _file_int(file_cfg: dict[str, Any], key: str, default: int) -> int:
    if key not in file_cfg:
        return default
    try:
        return int(file_cfg.get(key))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _env_or_file_bool(env_name: str, file_cfg: dict[str, Any], key: str, default: bool) -> bool:
    """Priority: env (if set) → config.json → default."""
    raw = _env(env_name, "")
    if raw:
        return raw.lower() in {"1", "true", "yes", "on"}
    return _file_bool(file_cfg, key, default)


def _env_or_file_int(env_name: str, file_cfg: dict[str, Any], key: str, default: int) -> int:
    raw = _env(env_name, "")
    if raw:
        try:
            return int(raw)
        except ValueError:
            pass
    return _file_int(file_cfg, key, default)


DEFAULT_OPENING_NUDGE = ""
# Fallback if config.json outbound.sip.voice missing (暖心学姐).
DEFAULT_OUTBOUND_VOICE = "saturn_zh_female_nuanxinxuejie_tob"
DEFAULT_TTS_SPEED = 80
DEFAULT_TTS_LOUDNESS = 0
DEFAULT_RTP_TX_PACE_ENABLED = False
DEFAULT_RTP_TX_PACE_HIGH_FRAMES = 50
DEFAULT_RTP_TX_QUEUE_FRAMES = 3000


def _env_or_file_str(
    env_names: tuple[str, ...] | str,
    file_cfg: dict[str, Any],
    key: str,
    default: str,
) -> str:
    names = (env_names,) if isinstance(env_names, str) else env_names
    for name in names:
        raw = _env(name, "")
        if raw:
            return raw
    if key in file_cfg:
        text = str(file_cfg.get(key) or "").strip()
        if text:
            return text
    return default


def resolve_outbound_voice() -> str:
    """Default Doubao voice_type (env > config.json outbound.sip.voice > default)."""
    file_cfg = _load_outbound_sip_file()
    return _env_or_file_str(
        ("SIP_TTS_VOICE", "OUTBOUND_VOICE", "DOUBAO_TTS_VOICE"),
        file_cfg,
        "voice",
        DEFAULT_OUTBOUND_VOICE,
    )


def resolve_tts_audio_params() -> dict[str, int]:
    """Doubao audio.output speed/loudness (env > config.json > defaults)."""
    file_cfg = _load_outbound_sip_file()
    # Prefer SIP_*; accept DOUBAO_* as alias when SIP_* unset.
    speed_raw = _env("SIP_TTS_SPEED", "") or _env("DOUBAO_TTS_SPEED", "")
    loud_raw = _env("SIP_TTS_LOUDNESS", "") or _env("DOUBAO_TTS_LOUDNESS", "")
    if speed_raw:
        try:
            speed = int(speed_raw)
        except ValueError:
            speed = _file_int(file_cfg, "tts_speed", DEFAULT_TTS_SPEED)
    else:
        speed = _file_int(file_cfg, "tts_speed", DEFAULT_TTS_SPEED)
    if loud_raw:
        try:
            loudness = int(loud_raw)
        except ValueError:
            loudness = _file_int(file_cfg, "tts_loudness", DEFAULT_TTS_LOUDNESS)
    else:
        loudness = _file_int(file_cfg, "tts_loudness", DEFAULT_TTS_LOUDNESS)
    return {
        "speed": max(-50, min(100, speed)),
        "loudness": max(-50, min(100, loudness)),
    }


def resolve_rtp_tx_settings() -> dict[str, Any]:
    """RTP TX soft-pace + queue (env > config.json outbound.sip > defaults)."""
    file_cfg = _load_outbound_sip_file()
    pace_enabled = _env_or_file_bool(
        "SIP_RTP_TX_PACE_ENABLED",
        file_cfg,
        "rtp_tx_pace_enabled",
        DEFAULT_RTP_TX_PACE_ENABLED,
    )
    pace_high = max(
        1,
        _env_or_file_int(
            "SIP_RTP_TX_PACE_HIGH_FRAMES",
            file_cfg,
            "rtp_tx_pace_high_frames",
            DEFAULT_RTP_TX_PACE_HIGH_FRAMES,
        ),
    )
    queue_frames = max(
        1,
        _env_or_file_int(
            "SIP_RTP_TX_QUEUE_FRAMES",
            file_cfg,
            "rtp_tx_queue_frames",
            DEFAULT_RTP_TX_QUEUE_FRAMES,
        ),
    )
    if pace_high >= queue_frames:
        pace_high = max(1, queue_frames // 2)
    return {
        "pace_enabled": pace_enabled,
        "pace_high_frames": pace_high,
        "queue_frames": queue_frames,
    }


def wrap_opening_as_speak_query(opening_line: str) -> str:
    """Deprecated: opening uses DuplexVoiceClient.say_hello (SayHello event)."""
    return (opening_line or "").strip().strip("「」").strip()


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
    # ~60 concurrent needs ≥60 ports; default 80 with headroom.
    rtp_port_count: int = 80
    from_user: str = DEFAULT_ANI
    called_prefix: str = DEFAULT_CALLED_PREFIX
    ring_timeout_sec: float = 45.0
    # Max media hold after answer when agent does not hang up earlier.
    max_call_sec: float = 600.0
    # No final user ASR for this long → local hangup (0 = disabled).
    user_idle_hangup_sec: float = 300.0
    # SIP TCP double-CRLF keepalive interval (avoid NAT/LB idle drop ~10-30s).
    sip_keepalive_sec: float = 10.0
    # Optional recording directory; empty = disabled.
    record_dir: str = ""
    # After answer, speak explicit opening_line (scenario) via Duplex text query.
    ai_speak_first: bool = True
    # Fallback only; urge_auth prefers CallTask.opening_line from prompt builder.
    opening_nudge: str = DEFAULT_OPENING_NUDGE
    # Doubao voice_type + audio.output.speed / loudness ([-50, 100]).
    voice: str = DEFAULT_OUTBOUND_VOICE
    tts_speed: int = DEFAULT_TTS_SPEED
    tts_loudness: int = DEFAULT_TTS_LOUDNESS
    # Soft 1x RTP TX pacing (also in config.json outbound.sip).
    rtp_tx_pace_enabled: bool = DEFAULT_RTP_TX_PACE_ENABLED
    rtp_tx_pace_high_frames: int = DEFAULT_RTP_TX_PACE_HIGH_FRAMES
    rtp_tx_queue_frames: int = DEFAULT_RTP_TX_QUEUE_FRAMES

    def is_configured(self) -> bool:
        return bool(self.dst_host and self.src_ip and self.from_user)

    def with_peer(self, host: str, port: int | None = None) -> "SipConfig":
        """Return a copy targeting a specific SIP peer (from traffic pool)."""
        return replace(
            self,
            dst_host=(host or self.dst_host).strip(),
            dst_port=int(port if port is not None else self.dst_port),
        )


def load_sip_config() -> SipConfig:
    """Load SIP settings: env SIP_* > config.json outbound.sip > defaults."""
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

    # Prefer first pool peer as default dst when SIP_DST_HOST unset.
    from backend.sip.traffic import load_sip_peers

    peers = load_sip_peers()
    default_host = peers[0][0] if peers else "124.70.154.200"
    default_port = peers[0][1] if peers else 5060

    rtp = resolve_rtp_tx_settings()
    audio = resolve_tts_audio_params()
    voice = resolve_outbound_voice()

    return SipConfig(
        dst_host=_env("SIP_DST_HOST", default_host) or default_host,
        dst_port=_env_int("SIP_DST_PORT", default_port),
        src_ip=_env("SIP_SRC_IP", "1.13.182.189"),
        bind_ip=_env("SIP_BIND_IP", "0.0.0.0"),
        src_sip_port=_env_int("SIP_SRC_PORT", 5070),
        rtp_port=_env_int("SIP_RTP_PORT", 10000),
        rtp_port_count=_env_int("SIP_RTP_PORT_COUNT", 80),
        from_user=ani,
        called_prefix=prefix,
        ring_timeout_sec=_env_float("SIP_RING_TIMEOUT", 45.0),
        max_call_sec=_env_float("SIP_MAX_CALL_SEC", 600.0),
        user_idle_hangup_sec=_env_float("SIP_USER_IDLE_HANGUP_SEC", 300.0),
        sip_keepalive_sec=_env_float("SIP_KEEPALIVE_SEC", 10.0),
        record_dir=_env("SIP_RECORD_DIR", ""),
        ai_speak_first=ai_speak_first,
        opening_nudge=opening_nudge,
        voice=voice,
        tts_speed=int(audio["speed"]),
        tts_loudness=int(audio["loudness"]),
        rtp_tx_pace_enabled=bool(rtp["pace_enabled"]),
        rtp_tx_pace_high_frames=int(rtp["pace_high_frames"]),
        rtp_tx_queue_frames=int(rtp["queue_frames"]),
    )
