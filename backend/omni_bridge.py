# -*- coding: utf-8 -*-
"""
DashScope Qwen Omni realtime bridge for browser websocket clients.

Browser <-> FastAPI websocket <-> DashScope realtime websocket
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import socket
import time
import uuid
from typing import Any

import websockets
from fastapi import WebSocket

from backend.hush import create_hush_session, hush_enabled, hush_status_payload
from backend.hush.manager import hush_manager_error
from backend.monitor_snapshot import build_monitor_snapshot, pcm16le_to_wav_bytes
from backend.vad import (
    VadEvent,
    create_vad_backend,
    is_local_semantic_vad_provider,
    vad_provider,
    vad_status_on_init_failure,
)
from backend.vad.config import resolve_vad_provider
from config.prompt_templates import DEFAULT_SYSTEM_ROLE

LOG = logging.getLogger(__name__)


DEFAULT_CN_URL = "wss://dashscope.aliyuncs.com/api-ws/v1/realtime"
DEFAULT_INTL_URL = "wss://dashscope-intl.aliyuncs.com/api-ws/v1/realtime"
DEFAULT_MODEL = "qwen3-omni-flash-realtime"
DEFAULT_VOICE = "Ethan"
DEFAULT_TURN_DETECTION_TYPE = "server_vad"
DEFAULT_SMOOTH_OUTPUT = True
DEFAULT_TURN_DETECTION_THRESHOLD_FLASH = 0.1
DEFAULT_TURN_DETECTION_SILENCE_MS_FLASH = 900
DEFAULT_TURN_DETECTION_THRESHOLD_35 = 0.7
DEFAULT_TURN_DETECTION_SILENCE_MS_35 = 1100
OMNI_INSTRUCTIONS_TONE_HINT = "情绪语气平缓，像电话访谈一样自然对话。"


def _normalize_model(model: str) -> str:
    return (model or "").strip().lower()


def _is_qwen35_omni_realtime(model: str) -> bool:
    """Qwen3.5-Omni-Realtime series, e.g. qwen3.5-omni-flash-realtime / qwen3.5-omni-plus-realtime."""
    m = _normalize_model(model)
    return m.startswith("qwen3.5-omni-") and "realtime" in m


def _supports_smooth_output(model: str) -> bool:
    """smooth_output applies only to Qwen3-Omni-Flash-Realtime."""
    return "qwen3-omni-flash-realtime" in _normalize_model(model)


def _supports_semantic_vad(model: str) -> bool:
    return _is_qwen35_omni_realtime(model)


def _default_turn_detection_threshold(model: str) -> float:
    if _is_qwen35_omni_realtime(model):
        return DEFAULT_TURN_DETECTION_THRESHOLD_35
    return DEFAULT_TURN_DETECTION_THRESHOLD_FLASH


def _default_turn_detection_silence_ms(model: str) -> int:
    if _is_qwen35_omni_realtime(model):
        return DEFAULT_TURN_DETECTION_SILENCE_MS_35
    return DEFAULT_TURN_DETECTION_SILENCE_MS_FLASH


def _parse_turn_detection_threshold(value: Any, model: str) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return _default_turn_detection_threshold(model)
    return max(-1.0, min(1.0, parsed))


def _parse_turn_detection_silence_ms(value: Any, model: str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return _default_turn_detection_silence_ms(model)
    return max(200, min(6000, parsed))


def _parse_smooth_output(value: Any) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1", "yes", "colloquial", "口语", "口语化"}:
            return True
        if normalized in {"false", "0", "no", "formal", "书面", "书面化"}:
            return False
        if normalized in {"null", "auto", "none", "自动"}:
            return None
    return DEFAULT_SMOOTH_OUTPUT


def _resolve_use_local_smart_turn(cfg: dict[str, Any], model: str) -> bool:
    mode = (cfg.get("omni_vad_mode") or "").strip().lower()
    if mode in {"local", "smart_turn_v3", "smart_turn", "livekit_turn_detector", "livekit_turn_v1", "livekit"}:
        return True
    if mode in {"dashscope", "server", "server_vad", "semantic_vad", "alibaba", "ali"}:
        return False
    if _is_qwen35_omni_realtime(model):
        return False
    return _omni_use_local_smart_turn(cfg)


def _omni_use_local_smart_turn(cfg: dict[str, Any] | None = None) -> bool:
    """
    Whether Omni uses in-process Smart Turn v3 instead of DashScope server VAD.

    OMNI_VAD=server → always server VAD
    OMNI_VAD=smart_turn_v3|livekit_turn_detector|local → always local VAD
    unset → follow global vad.provider (smart_turn_v3 / livekit_turn_detector enable local VAD)
    """
    override = (os.environ.get("OMNI_VAD", "") or "").strip().lower()
    if override in {"server", "server_vad", "dashscope"}:
        return False
    if override in {"smart_turn_v3", "smart_turn", "local", "livekit_turn_detector", "livekit_turn_v1", "livekit"}:
        return True
    return is_local_semantic_vad_provider(resolve_vad_provider(cfg=cfg or {}))


def _build_dashscope_url(region: str) -> str:
    if region == "intl":
        return DEFAULT_INTL_URL
    return DEFAULT_CN_URL


def _tts_barge_suppress_ms() -> int:
    """Same knob as t2sbg: debug.t2s_tts_barge_suppress_ms / T2S_TTS_BARGE_SUPPRESS_MS."""
    try:
        from backend.server import T2S_TTS_BARGE_SUPPRESS_MS

        return int(T2S_TTS_BARGE_SUPPRESS_MS)
    except Exception:
        return 0


def _safe_json_loads(text: str) -> dict[str, Any]:
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            return obj
    except json.JSONDecodeError:
        pass
    return {}


class OmniRealtimeBridge:
    """Bridge one browser websocket to one DashScope omni websocket session."""

    def __init__(self, websocket: WebSocket):
        self.client_ws = websocket
        self.conn_id = uuid.uuid4().hex
        self.omni_ws: websockets.WebSocketClientProtocol | None = None
        self.omni_recv_task: asyncio.Task | None = None
        self.closed = False

        # Debug recording (VAD feed + Omni uplink), same semantics as t2sbg monitor.
        self._vad_turn_buffer = bytearray()
        self._vad_merged_buffer = bytearray()
        self._omni_turn_buffer = bytearray()
        self._omni_merged_buffer = bytearray()
        self._vad_turn_pcm_history: list[bytes] = []
        self._omni_turn_pcm_history: list[bytes] = []
        self._vad_segments = 0
        self._omni_segments = 0
        self._turn_capture_active = False

        # runtime settings (can be overridden by first browser config frame)
        self.api_key = os.environ.get("DASHSCOPE_API_KEY", "")
        self.region = "cn"
        self.model = os.environ.get("OMNI_MODEL", DEFAULT_MODEL).strip() or DEFAULT_MODEL
        self.voice = DEFAULT_VOICE
        self.instructions = DEFAULT_SYSTEM_ROLE
        self.smooth_output: bool | None = DEFAULT_SMOOTH_OUTPUT
        self.turn_detection_type = DEFAULT_TURN_DETECTION_TYPE
        self.turn_detection_threshold = _default_turn_detection_threshold(self.model)
        self.turn_detection_silence_duration_ms = _default_turn_detection_silence_ms(self.model)
        self._use_local_smart_turn = False
        self._local_vad = None
        self._active_vad_provider = vad_provider()
        self._hush = create_hush_session() if hush_enabled() else None
        # First TTS PCM sent to browser (monotonic); 0 = not in a playback burst yet.
        self._tts_first_audio_mono: float = 0.0

    def _tts_barge_suppress_active(self) -> bool:
        """True during lead-in after first downstream TTS chunk (echo barge-in guard)."""
        ms = _tts_barge_suppress_ms()
        if ms <= 0:
            return False
        if self._tts_first_audio_mono <= 0:
            return False
        return (time.monotonic() - self._tts_first_audio_mono) * 1000.0 < float(ms)

    async def _send_client_json(self, payload: dict[str, Any]) -> None:
        try:
            await self.client_ws.send_text(json.dumps(payload, ensure_ascii=False))
        except Exception:
            pass
        else:
            from backend.demo_asr_broadcast import DEMO_ASR_EVENT_TYPES, schedule_demo_asr_broadcast

            if payload.get("type") in DEMO_ASR_EVENT_TYPES:
                schedule_demo_asr_broadcast(payload)

    async def _send_client_audio(self, pcm_s16le: bytes) -> None:
        try:
            await self.client_ws.send_bytes(pcm_s16le)
        except Exception:
            pass

    async def _begin_turn_capture(self) -> None:
        if self._turn_capture_active:
            return
        self._turn_capture_active = True
        self._vad_turn_buffer.clear()
        self._omni_turn_buffer.clear()
        await self._send_client_json({"type": "voice_input_started"})

    def _finalize_turn_capture(self) -> tuple[str, str]:
        """Snapshot current turn into merged buffers; return per-turn WAV b64 for preview."""
        vad_last_turn_wav_b64 = ""
        omni_last_turn_wav_b64 = ""
        if self._vad_turn_buffer:
            snap_v = bytes(self._vad_turn_buffer)
            self._vad_turn_pcm_history.append(snap_v)
            vad_last_turn_wav_b64 = base64.b64encode(pcm16le_to_wav_bytes(snap_v)).decode("ascii")
            self._vad_merged_buffer.extend(self._vad_turn_buffer)
            self._vad_segments += 1
            self._vad_turn_buffer.clear()
        if self._omni_turn_buffer:
            snap_o = bytes(self._omni_turn_buffer)
            self._omni_turn_pcm_history.append(snap_o)
            omni_last_turn_wav_b64 = base64.b64encode(pcm16le_to_wav_bytes(snap_o)).decode("ascii")
            self._omni_merged_buffer.extend(self._omni_turn_buffer)
            self._omni_segments += 1
            self._omni_turn_buffer.clear()
        self._turn_capture_active = False
        return vad_last_turn_wav_b64, omni_last_turn_wav_b64

    async def _send_debug_audio_preview(
        self,
        *,
        vad_last_turn_wav_b64: str = "",
        omni_last_turn_wav_b64: str = "",
    ) -> None:
        vad_pcm = bytes(self._vad_merged_buffer)
        omni_pcm = bytes(self._omni_merged_buffer)
        vad_wav_b64 = (
            base64.b64encode(pcm16le_to_wav_bytes(vad_pcm)).decode("ascii") if vad_pcm else ""
        )
        omni_wav_b64 = (
            base64.b64encode(pcm16le_to_wav_bytes(omni_pcm)).decode("ascii") if omni_pcm else ""
        )
        payload: dict[str, object] = {
            "type": "debug_audio_preview",
            "vad_wav_base64": vad_wav_b64,
            "asr_wav_base64": omni_wav_b64,
            "vad_segments": self._vad_segments,
            "asr_segments": self._omni_segments,
            "vad_duration_sec": round(len(vad_pcm) / 2 / 16000, 3),
            "asr_duration_sec": round(len(omni_pcm) / 2 / 16000, 3),
        }
        if vad_last_turn_wav_b64:
            payload["vad_last_turn_wav_base64"] = vad_last_turn_wav_b64
        if omni_last_turn_wav_b64:
            payload["asr_last_turn_wav_base64"] = omni_last_turn_wav_b64
        await self._send_client_json(payload)

    def build_monitor_snapshot(
        self,
        *,
        transcript_html: str,
        log_html: str,
        full_merged_wav_b64: str,
        full_turn_wavs_b64: list[str],
    ) -> dict[str, object]:
        return build_monitor_snapshot(
            conn_id=self.conn_id,
            vad_merged=bytes(self._vad_merged_buffer),
            asr_merged=bytes(self._omni_merged_buffer),
            vad_turn_history=list(self._vad_turn_pcm_history),
            asr_turn_history=list(self._omni_turn_pcm_history),
            vad_segments=self._vad_segments,
            asr_segments=self._omni_segments,
            transcript_html=transcript_html,
            log_html=log_html,
            full_merged_wav_b64=full_merged_wav_b64,
            full_turn_wavs_b64=full_turn_wavs_b64,
            source_mode="omni",
        )

    async def _send_omni_event(self, event: dict[str, Any]) -> None:
        if not self.omni_ws:
            return
        event["event_id"] = f"event_{int(time.time() * 1000)}"
        await self.omni_ws.send(json.dumps(event, ensure_ascii=False))

    def _dashscope_ws_connect_kwargs(self) -> dict[str, Any]:
        """DashScope WSS can hang on IPv6 in some networks; prefer IPv4 unless overridden."""
        kwargs: dict[str, Any] = {}
        family_override = (os.environ.get("OMNI_WS_FAMILY") or "inet4").strip().lower()
        if family_override in {"inet4", "ipv4", "v4", "4"}:
            kwargs["family"] = socket.AF_INET
        elif family_override in {"inet6", "ipv6", "v6", "6"}:
            kwargs["family"] = socket.AF_INET6
        try:
            kwargs["open_timeout"] = float(os.environ.get("OMNI_WS_OPEN_TIMEOUT", "20"))
        except ValueError:
            kwargs["open_timeout"] = 20.0
        return kwargs

    async def _connect_omni(self) -> None:
        if not self.api_key:
            raise RuntimeError("DASHSCOPE_API_KEY is not configured")
        url = f"{_build_dashscope_url(self.region)}?model={self.model}"
        headers = {"Authorization": f"Bearer {self.api_key}"}
        try:
            self.omni_ws = await websockets.connect(
                url,
                additional_headers=headers,
                **self._dashscope_ws_connect_kwargs(),
            )
        except TimeoutError as exc:
            raise RuntimeError(
                "DashScope Omni websocket handshake timed out "
                "(check network/VPN; IPv6 to dashscope.aliyuncs.com may fail — default uses IPv4 via OMNI_WS_FAMILY=inet4)"
            ) from exc

    def _build_omni_instructions(self) -> str:
        base = (self.instructions or "").strip()
        if "语气平缓" in base or "情绪平缓" in base or OMNI_INSTRUCTIONS_TONE_HINT in base:
            return base
        tone_section = f"【语气要求】{OMNI_INSTRUCTIONS_TONE_HINT}"
        return f"{base}\n\n{tone_section}" if base else tone_section

    async def _update_omni_session(self) -> None:
        # Qwen3.5 + local Smart Turn: omit turn_detection (DashScope default server_vad).
        # Flash + local Smart Turn: turn_detection=null (manual commit).
        session: dict[str, Any] = {
            "modalities": ["text", "audio"],
            "voice": self.voice,
            "instructions": self._build_omni_instructions(),
            "input_audio_format": "pcm",
            "output_audio_format": "pcm",
            "input_audio_transcription": {"model": "gummy-realtime-v1"},
        }
        if _is_qwen35_omni_realtime(self.model) and self._use_local_smart_turn:
            pass
        elif self._use_local_smart_turn or self.turn_detection_type == "manual":
            session["turn_detection"] = None
        else:
            turn_detection: dict[str, Any] = {
                "type": self.turn_detection_type,
                "threshold": self.turn_detection_threshold,
                "silence_duration_ms": self.turn_detection_silence_duration_ms,
            }
            if _supports_smooth_output(self.model):
                turn_detection["prefix_padding_ms"] = 500
            session["turn_detection"] = turn_detection
        if _supports_smooth_output(self.model):
            session["smooth_output"] = self.smooth_output
        await self._send_omni_event({"type": "session.update", "session": session})

    async def _omni_receive_loop(self) -> None:
        if not self.omni_ws:
            return
        try:
            async for raw in self.omni_ws:
                if not isinstance(raw, str):
                    continue
                event = _safe_json_loads(raw)
                if not event:
                    continue
                t = event.get("type", "")
                if t == "error":
                    await self._send_client_json(
                        {"type": "error", "message": str(event.get("error", "omni error"))}
                    )
                elif t == "response.audio.delta":
                    delta = event.get("delta", "")
                    if delta:
                        try:
                            pcm = base64.b64decode(delta)
                            if pcm and self._tts_first_audio_mono <= 0:
                                self._tts_first_audio_mono = time.monotonic()
                            await self._send_client_audio(pcm)
                        except Exception:
                            pass
                elif t == "conversation.item.input_audio_transcription.delta":
                    preview = (event.get("text", "") or "") + (event.get("stash", "") or "")
                    if preview.strip():
                        await self._send_client_json(
                            {"type": "asr", "text": preview, "is_interim": True}
                        )
                elif t == "conversation.item.input_audio_transcription.completed":
                    text = event.get("transcript", "") or ""
                    await self._send_client_json({"type": "asr", "text": text, "is_interim": False})
                    await self._send_client_json({"type": "local_asr_text", "text": text})
                elif t == "response.audio_transcript.delta":
                    await self._send_client_json(
                        {
                            "type": "chat",
                            "content": event.get("delta", "") or "",
                            "question_id": event.get("response_id", "") or "",
                            "reply_id": event.get("item_id", "") or "",
                        }
                    )
                elif t == "response.audio_transcript.done":
                    # Send final line for easier UI completion handling.
                    await self._send_client_json(
                        {
                            "type": "chat_end",
                            "question_id": event.get("response_id", "") or "",
                            "reply_id": event.get("item_id", "") or "",
                            "transcript": event.get("transcript", "") or "",
                        }
                    )
                elif t == "response.created":
                    self._tts_first_audio_mono = 0.0
                    await self._send_client_json({"type": "tts_start"})
                elif t == "response.done":
                    await self._send_client_json({"type": "tts_end"})
                elif t == "input_audio_buffer.speech_started" and not self._use_local_smart_turn:
                    await self._begin_turn_capture()
                    if self._tts_barge_suppress_active():
                        suppress_ms = _tts_barge_suppress_ms()
                        LOG.info(
                            "omni speech_started ignored (TTS lead-in %sms)",
                            suppress_ms,
                        )
                        await self._send_client_json(
                            {
                                "type": "server_log",
                                "level": "info",
                                "message": "Omni speech_started ignored (TTS lead-in window)",
                                "ms": suppress_ms,
                            }
                        )
                    else:
                        await self._send_omni_event({"type": "response.cancel"})
                        await self._send_client_json({"type": "vad_state", "provider": "omni", "state": "nonidle"})
                        await self._send_client_json(
                            {
                                "type": "barge_in",
                                "source": "omni",
                                "reason": "user_speech",
                                "reason_label": "用户打断（检测到说话）",
                            }
                        )
                elif t == "input_audio_buffer.speech_stopped" and not self._use_local_smart_turn:
                    vad_b64, omni_b64 = self._finalize_turn_capture()
                    await self._send_debug_audio_preview(
                        vad_last_turn_wav_b64=vad_b64,
                        omni_last_turn_wav_b64=omni_b64,
                    )
                    await self._send_client_json({"type": "vad_state", "provider": "omni", "state": "idle"})
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            await self._send_client_json({"type": "error", "message": f"omni receive loop error: {exc}"})

    async def _on_local_vad_event(self, ev: VadEvent) -> None:
        payload: dict[str, Any] = {
            "type": "vad_state",
            "provider": ev.provider,
            "state": ev.state,
        }
        if ev.meta:
            payload.update(ev.meta)
        await self._send_client_json(payload)

        if ev.kind == "speech_start":
            await self._begin_turn_capture()
            if self._tts_barge_suppress_active():
                suppress_ms = _tts_barge_suppress_ms()
                LOG.info(
                    "omni VAD speech_start ignored (TTS lead-in %sms)",
                    suppress_ms,
                )
                await self._send_client_json(
                    {
                        "type": "server_log",
                        "level": "info",
                        "message": "VAD speech_start ignored (TTS lead-in window)",
                        "ms": suppress_ms,
                    }
                )
            else:
                await self._send_omni_event({"type": "response.cancel"})
                await self._send_client_json(
                    {
                        "type": "barge_in",
                        "source": ev.provider,
                        "reason": "user_speech",
                        "reason_label": "用户打断（检测到说话）",
                    }
                )
        elif ev.kind == "turn_end_candidate":
            vad_b64, omni_b64 = self._finalize_turn_capture()
            await self._send_debug_audio_preview(
                vad_last_turn_wav_b64=vad_b64,
                omni_last_turn_wav_b64=omni_b64,
            )
            await self._send_omni_event({"type": "input_audio_buffer.commit"})
            await self._send_omni_event(
                {
                    "type": "response.create",
                    "response": {"modalities": ["text", "audio"]},
                }
            )
            await self._send_client_json(
                {
                    "type": "voice_input_complete_suggested",
                    "source": ev.provider,
                }
            )

    async def start(self, initial_cfg: dict[str, Any] | None = None) -> None:
        cfg = initial_cfg or {}
        self.region = (cfg.get("region") or self.region or "cn").strip().lower()
        self.model = (cfg.get("omni_model") or self.model or DEFAULT_MODEL).strip()
        self.voice = (cfg.get("omni_voice") or self.voice or DEFAULT_VOICE).strip()
        self.instructions = (cfg.get("system_role") or self.instructions or "").strip() or self.instructions
        if "omni_smooth_output" in cfg:
            self.smooth_output = _parse_smooth_output(cfg.get("omni_smooth_output"))

        self.turn_detection_threshold = _parse_turn_detection_threshold(
            cfg.get("omni_turn_detection_threshold"),
            self.model,
        )
        self.turn_detection_silence_duration_ms = _parse_turn_detection_silence_ms(
            cfg.get("omni_turn_detection_silence_duration_ms"),
            self.model,
        )

        self._use_local_smart_turn = _resolve_use_local_smart_turn(cfg, self.model)
        self._active_vad_provider = (
            resolve_vad_provider(cfg=cfg) if self._use_local_smart_turn else "dashscope"
        )
        LOG.info(
            "Omni VAD session: model=%s use_local=%s provider=%s omni_vad_mode=%r vad_provider=%r local_vad_provider=%r",
            self.model,
            self._use_local_smart_turn,
            self._active_vad_provider,
            cfg.get("omni_vad_mode"),
            cfg.get("vad_provider"),
            cfg.get("local_vad_provider"),
        )
        embedded_asr = self._active_vad_provider == "livekit_turn_detector"
        livekit_stt = (cfg.get("livekit_stt_backend") or cfg.get("stt_backend") or "").strip() or None
        livekit_th = cfg.get("livekit_turn_threshold")
        if livekit_th is None:
            livekit_th = cfg.get("threshold")
        if _is_qwen35_omni_realtime(self.model):
            if self._use_local_smart_turn:
                self._local_vad = create_vad_backend(
                    self._active_vad_provider,
                    force_embedded_asr=embedded_asr or None,
                    livekit_stt_backend=livekit_stt,
                    livekit_turn_threshold=livekit_th,
                )
                if self._local_vad is None:
                    LOG.warning("Omni local VAD unavailable; falling back to semantic_vad")
                    self._use_local_smart_turn = False
                    self._active_vad_provider = "dashscope"
                    self.turn_detection_type = "semantic_vad"
            else:
                td = (cfg.get("omni_turn_detection_type") or "semantic_vad").strip()
                if td == "semantic_vad" and not _supports_semantic_vad(self.model):
                    td = DEFAULT_TURN_DETECTION_TYPE
                if td in {"server_vad", "semantic_vad"}:
                    self.turn_detection_type = td
                self._local_vad = None
        elif self._use_local_smart_turn:
            self.turn_detection_type = "manual"
            self._local_vad = create_vad_backend(
                self._active_vad_provider,
                force_embedded_asr=embedded_asr or None,
                livekit_stt_backend=livekit_stt,
                livekit_turn_threshold=livekit_th,
            )
            if self._local_vad is None:
                LOG.warning("Omni local VAD unavailable; falling back to server_vad")
                self._use_local_smart_turn = False
                self._active_vad_provider = "dashscope"
                self.turn_detection_type = DEFAULT_TURN_DETECTION_TYPE
        else:
            td = (cfg.get("omni_turn_detection_type") or self.turn_detection_type).strip()
            if td == "semantic_vad" and not _supports_semantic_vad(self.model):
                LOG.warning("semantic_vad requested but unsupported for model=%s; using server_vad", self.model)
                td = DEFAULT_TURN_DETECTION_TYPE
            if td in {"server_vad", "semantic_vad", "manual"}:
                self.turn_detection_type = td
            self._local_vad = None

        await self._connect_omni()
        await self._update_omni_session()
        self.omni_recv_task = asyncio.create_task(self._omni_receive_loop())

        vad_mode = "dashscope"
        if self._use_local_smart_turn and self._local_vad is not None:
            try:
                vad_status = await self._local_vad.start_session(uuid.uuid4().hex)
                await self._send_client_json({"type": "vad_status", **vad_status})
                vad_mode = self._active_vad_provider
            except Exception as exc:
                LOG.warning("Omni local VAD start failed: %s", exc)
                failed_provider = self._active_vad_provider
                await self._local_vad.close_session()
                self._local_vad = None
                self._use_local_smart_turn = False
                self._active_vad_provider = "dashscope"
                self.turn_detection_type = DEFAULT_TURN_DETECTION_TYPE
                await self._update_omni_session()
                await self._send_client_json(
                    {
                        "type": "vad_status",
                        **vad_status_on_init_failure(failed_provider, str(exc)),
                    }
                )

        await self._send_client_json(
            {
                "type": "ready",
                "mode": "omni",
                "omni_backend": "qwen",
                "omni_model": self.model,
                "omni_voice": self.voice,
                "omni_smooth_output": self.smooth_output if _supports_smooth_output(self.model) else None,
                "omni_vad_mode": self._active_vad_provider if self._use_local_smart_turn else "dashscope",
                "omni_turn_detection": (
                    None
                    if self._use_local_smart_turn
                    else {
                        "type": self.turn_detection_type,
                        "threshold": self.turn_detection_threshold,
                        "silence_duration_ms": self.turn_detection_silence_duration_ms,
                    }
                ),
                "region": self.region,
                "vad_mode": vad_mode,
                "hush": hush_status_payload(
                    ready=self._hush is not None,
                    error=hush_manager_error(),
                ),
            }
        )

    async def handle_client_text(self, raw: str) -> None:
        obj = _safe_json_loads(raw)
        if not obj:
            return
        t = obj.get("type")
        # Keep compatibility with existing t2sbg text input.
        if t == "chat_text_query":
            content = (obj.get("content") or "").strip()
            if not content:
                return
            # For text-only manual message, create a response directly.
            await self._send_omni_event(
                {
                    "type": "response.create",
                    "response": {
                        "modalities": ["text", "audio"],
                        "instructions": f"请回复用户这句话：{content}",
                    },
                }
            )
            await self._send_client_json({"type": "chat_text_query_sent", "reason": "omni_text_proxy"})
            await self._send_client_json({"type": "server_log", "level": "info", "message": "send_text routed to omni only"})
        elif t == "response_cancel":
            await self._send_omni_event({"type": "response.cancel"})
        elif t == "config_audio":
            # Omni side expects pcm/16k from browser. Keep ack for UI logs.
            await self._send_client_json(
                {
                    "type": "audio_config_applied",
                    "input_sample_rate": int(obj.get("input_sample_rate", 16000) or 16000),
                    "vad_asr_target_sample_rate": 16000,
                    "resample_enabled": False,
                    "resampler": "none",
                }
            )

    def _denoise_uplink_pcm(self, pcm_s16le: bytes) -> bytes:
        if self._hush is None:
            return pcm_s16le
        return self._hush.process_pcm_s16le(pcm_s16le)

    async def handle_client_audio(self, pcm_s16le: bytes) -> None:
        # Browser sends raw PCM16 mono 16k chunks; optional Hush denoise before uplink.
        uplink_pcm = self._denoise_uplink_pcm(pcm_s16le)
        if not uplink_pcm:
            return
        if self._turn_capture_active:
            self._omni_turn_buffer.extend(uplink_pcm)
            if self._local_vad is not None:
                self._vad_turn_buffer.extend(uplink_pcm)
        audio_b64 = base64.b64encode(uplink_pcm).decode("ascii")
        await self._send_omni_event({"type": "input_audio_buffer.append", "audio": audio_b64})
        if self._local_vad is not None:
            try:
                events = await self._local_vad.feed_pcm_s16le_16k(uplink_pcm)
                for ev in events:
                    await self._on_local_vad_event(ev)
            except Exception as exc:
                LOG.warning("Omni local VAD feed failed: %s", exc)

    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        if self.omni_recv_task and not self.omni_recv_task.done():
            self.omni_recv_task.cancel()
            try:
                await self.omni_recv_task
            except Exception:
                pass
        if self._local_vad is not None:
            try:
                await self._local_vad.close_session()
            except Exception:
                pass
            self._local_vad = None
        if self._hush is not None:
            try:
                self._hush.close()
            except Exception:
                pass
            self._hush = None
        if self.omni_ws:
            try:
                await self.omni_ws.close()
            except Exception:
                pass
            self.omni_ws = None
