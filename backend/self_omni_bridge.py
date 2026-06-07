# -*- coding: utf-8 -*-
"""
Self-deployed Omni bridge for browser websocket clients.

Browser <-> FastAPI websocket <-> self-hosted Omni websocket (batch turn protocol).

Protocol (upstream server, see ws_client2.py):
  send: {"audio_base64": "<wav base64>"}
  recv: {"event": "generating"|"speaking"|"completed"|"error", ...}
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import time
import uuid
from pathlib import Path
from typing import Any

import numpy as np
import websockets
from fastapi import WebSocket

from backend.hush import create_hush_session, hush_enabled, hush_status_payload
from backend.hush.manager import hush_manager_error
from backend.monitor_snapshot import build_monitor_snapshot, pcm16le_to_wav_bytes
from backend.vad import (
    VadEvent,
    create_vad_backend,
    vad_status_on_init_failure,
)

LOG = logging.getLogger(__name__)

LOCAL_VAD_PROVIDER = "smart_turn_v3"
SELF_OMNI_TTS_SAMPLE_RATE = 24000


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _self_omni_ws_url() -> str:
    env = (os.environ.get("SELF_OMNI_WS_URL") or "").strip()
    if env:
        return env
    try:
        cfg_path = _repo_root() / "config.json"
        if cfg_path.exists():
            cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
            omni = cfg.get("omni") or {}
            return str(omni.get("self_deployed_ws_url") or "").strip()
    except Exception:
        pass
    return ""


def _tts_barge_suppress_ms() -> int:
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


def _float32_to_pcm16le(audio_f32: bytes) -> bytes:
    arr = np.frombuffer(audio_f32, dtype=np.float32)
    if arr.size == 0:
        return b""
    clipped = np.clip(arr, -1.0, 1.0)
    return (clipped * 32767.0).astype(np.int16).tobytes()


class SelfDeployedOmniBridge:
    """Bridge one browser websocket to self-deployed Omni (one WS request per turn)."""

    def __init__(self, websocket: WebSocket):
        self.client_ws = websocket
        self.conn_id = uuid.uuid4().hex
        self.closed = False

        self._vad_turn_buffer = bytearray()
        self._vad_merged_buffer = bytearray()
        self._omni_turn_buffer = bytearray()
        self._omni_merged_buffer = bytearray()
        self._vad_turn_pcm_history: list[bytes] = []
        self._omni_turn_pcm_history: list[bytes] = []
        self._vad_segments = 0
        self._omni_segments = 0
        self._turn_capture_active = False

        self.ws_url = _self_omni_ws_url()
        self.instructions = "你是芝麻背调的专业背景调查访谈员，请保持专业、亲和、严谨。"
        self._local_vad = None
        self._hush = create_hush_session() if hush_enabled() else None
        self._tts_first_audio_mono: float = 0.0

        self._turn_in_flight = False
        self._self_ws: websockets.WebSocketClientProtocol | None = None
        self._self_recv_task: asyncio.Task | None = None
        self._tts_started_for_turn = False

    def _tts_barge_suppress_active(self) -> bool:
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
            source_mode="omni-self",
        )

    async def _cancel_self_turn(self) -> None:
        if self._self_recv_task and not self._self_recv_task.done():
            self._self_recv_task.cancel()
            try:
                await self._self_recv_task
            except Exception:
                pass
        self._self_recv_task = None
        if self._self_ws:
            try:
                await self._self_ws.close()
            except Exception:
                pass
            self._self_ws = None
        self._turn_in_flight = False
        self._tts_started_for_turn = False
        self._tts_first_audio_mono = 0.0

    async def _handle_self_event(self, data: dict[str, Any]) -> bool:
        """Process one upstream event. Return False to stop receive loop."""
        event = data.get("event", "")
        source = str(data.get("source", "") or "")
        text = str(data.get("text", "") or "")

        if event == "generating":
            if text:
                if source in {"asr", "stage1", "user"}:
                    await self._send_client_json({"type": "asr", "text": text, "is_interim": True})
                else:
                    await self._send_client_json({"type": "chat", "content": text})
            return True

        if event == "speaking":
            audio_b64 = data.get("audio") or ""
            if audio_b64:
                if not self._tts_started_for_turn:
                    self._tts_started_for_turn = True
                    self._tts_first_audio_mono = 0.0
                    await self._send_client_json({"type": "tts_start"})
                try:
                    pcm = _float32_to_pcm16le(base64.b64decode(audio_b64))
                    if pcm:
                        if self._tts_first_audio_mono <= 0:
                            self._tts_first_audio_mono = time.monotonic()
                        await self._send_client_audio(pcm)
                except Exception as exc:
                    LOG.warning("Self Omni speaking decode failed: %s", exc)
            return True

        if event == "completed":
            if text:
                await self._send_client_json({"type": "asr", "text": text, "is_interim": False})
                await self._send_client_json({"type": "local_asr_text", "text": text})
            await self._send_client_json({"type": "chat_end", "transcript": text})
            await self._send_client_json({"type": "tts_end"})
            return False

        if event == "error":
            await self._send_client_json(
                {"type": "error", "message": str(data.get("message") or "self omni error")}
            )
            return False

        return True

    async def _self_receive_loop(self) -> None:
        if not self._self_ws:
            return
        try:
            while True:
                raw = await self._self_ws.recv()
                if not isinstance(raw, str):
                    continue
                data = _safe_json_loads(raw)
                if not data:
                    continue
                cont = await self._handle_self_event(data)
                if not cont:
                    break
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            await self._send_client_json({"type": "error", "message": f"self omni receive error: {exc}"})
        finally:
            self._turn_in_flight = False
            self._tts_started_for_turn = False

    async def _submit_turn_to_self_omni(self, wav_b64: str) -> None:
        if not self.ws_url:
            await self._send_client_json(
                {"type": "error", "message": "SELF_OMNI_WS_URL is not configured"}
            )
            return
        if self._turn_in_flight:
            await self._send_client_json(
                {"type": "server_log", "level": "info", "message": "self omni turn still in flight, skipped"}
            )
            return

        await self._cancel_self_turn()
        self._turn_in_flight = True
        try:
            self._self_ws = await websockets.connect(
                self.ws_url,
                ping_interval=None,
                ping_timeout=None,
                close_timeout=None,
            )
            await self._self_ws.send(json.dumps({"audio_base64": wav_b64}, ensure_ascii=False))
            self._self_recv_task = asyncio.create_task(self._self_receive_loop())
        except Exception as exc:
            self._turn_in_flight = False
            await self._send_client_json({"type": "error", "message": f"self omni connect failed: {exc}"})

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
                LOG.info("self omni VAD speech_start ignored (TTS lead-in %sms)", suppress_ms)
                await self._send_client_json(
                    {
                        "type": "server_log",
                        "level": "info",
                        "message": "VAD speech_start ignored (TTS lead-in window)",
                        "ms": suppress_ms,
                    }
                )
            else:
                await self._cancel_self_turn()
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
            await self._send_client_json(
                {"type": "voice_input_complete_suggested", "source": ev.provider}
            )
            if omni_b64:
                await self._submit_turn_to_self_omni(omni_b64)

    async def start(self, initial_cfg: dict[str, Any] | None = None) -> None:
        cfg = initial_cfg or {}
        self.instructions = (cfg.get("system_role") or self.instructions or "").strip() or self.instructions
        override_url = (cfg.get("self_omni_ws_url") or "").strip()
        if override_url:
            self.ws_url = override_url

        if not self.ws_url:
            raise RuntimeError("SELF_OMNI_WS_URL is not configured (required for self-deployed Omni)")

        self._local_vad = create_vad_backend()
        vad_mode = "smart_turn_v3"
        if self._local_vad is None:
            raise RuntimeError("Smart Turn v3 VAD is required for self-deployed Omni")
        try:
            vad_status = await self._local_vad.start_session(uuid.uuid4().hex)
            await self._send_client_json({"type": "vad_status", **vad_status})
        except Exception as exc:
            await self._local_vad.close_session()
            self._local_vad = None
            await self._send_client_json(
                {
                    "type": "vad_status",
                    **vad_status_on_init_failure(LOCAL_VAD_PROVIDER, str(exc)),
                }
            )
            raise RuntimeError(f"Self Omni VAD start failed: {exc}") from exc

        await self._send_client_json(
            {
                "type": "ready",
                "mode": "omni",
                "omni_backend": "self",
                "omni_model": "self-deployed",
                "self_omni_ws_url": self.ws_url,
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
        if t == "chat_text_query":
            await self._send_client_json(
                {
                    "type": "error",
                    "message": "自部署 Omni 暂不支持纯文本输入，请使用麦克风说话",
                }
            )
        elif t == "response_cancel":
            await self._cancel_self_turn()
        elif t == "config_audio":
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
        uplink_pcm = self._denoise_uplink_pcm(pcm_s16le)
        if not uplink_pcm:
            return
        if self._turn_capture_active:
            self._omni_turn_buffer.extend(uplink_pcm)
            if self._local_vad is not None:
                self._vad_turn_buffer.extend(uplink_pcm)
        if self._local_vad is not None:
            try:
                events = await self._local_vad.feed_pcm_s16le_16k(uplink_pcm)
                for ev in events:
                    await self._on_local_vad_event(ev)
            except Exception as exc:
                LOG.warning("Self Omni local VAD feed failed: %s", exc)

    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        await self._cancel_self_turn()
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
