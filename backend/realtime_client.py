# -*- coding: utf-8 -*-
"""
Doubao Realtime Voice API WebSocket client.

Manages connection, StartSession with system prompt, audio streaming,
and handles server events (ASR, TTS, barge-in).
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any, Callable

import websockets
from websockets.exceptions import ConnectionClosed
from websockets.protocol import State

from backend.protocol import (
    EVENT_ASR_ENDED,
    EVENT_ASR_INFO,
    EVENT_ASR_RESPONSE,
    EVENT_CHAT_ENDED,
    EVENT_CHAT_RESPONSE,
    EVENT_CHAT_TEXT_QUERY,
    EVENT_SESSION_FAILED,
    EVENT_SESSION_STARTED,
    EVENT_TTS_ENDED,
    EVENT_TTS_RESPONSE,
    EVENT_TTS_SENTENCE_START,
    EVENT_DIALOG_COMMON_ERROR,
    EVENT_FINISH_CONNECTION,
    EVENT_FINISH_SESSION,
    EVENT_START_CONNECTION,
    EVENT_START_SESSION,
    decode_server_frame,
    encode_audio_request,
    encode_client_json_event,
    new_session_id,
)

LOG = logging.getLogger(__name__)

WS_URL = "wss://openspeech.bytedance.com/api/v3/realtime/dialogue"

DEFAULT_START_SESSION_PAYLOAD = {
    # Align with official Python demo / docs semantics.
    "asr": {
        "audio_info": {
            "format": "speech_pcm",
            "sample_rate": 16000,
            "channel": 1,
        },
        "extra": {
            # end_smooth_window_ms: server VAD “user stop speaking” window, default 1500ms.
            "end_smooth_window_ms": 1500,
        },
    },
    "tts": {
        # Use an official high-quality voice; you can override via StartSession.
        "speaker": "zh_male_xiaotian_jupiter_bigtts",
        "audio_config": {
            "channel": 1,
            # Match official demo: 24k PCM; we still request s16le for browser playback.
            "format": "pcm_s16le",
            "sample_rate": 24000,
        },
    },
    "dialog": {
        "bot_name": "背调访谈员",
        "system_role": "",
        "speaking_style": "",
        "dialog_id": "",
        "extra": {
            # Model version: O2.0 canonical version id.
            "model": "1.2.1.1",
            # Input mode: mic audio; keep_alive is not needed when streaming audio continuously.
            "input_mod": "audio",
            # Close safety audit for demo (can be toggled later).
            "strict_audit": False,
            "audit_response": "当前为测试环境，如触发安全审核，请联系管理员。",
            # Allow some silence before timing out, primarily for text/audio_file modes.
            "recv_timeout": 10,
        },
    },
}


class RealtimeVoiceClient:
    """Async client for Doubao Realtime Voice API."""

    def __init__(
        self,
        app_id: str,
        access_key: str,
        *,
        on_asr: Callable[[str, bool], None] | None = None,
        on_asr_ended: Callable[[str], None] | None = None,
        on_tts_audio: Callable[[bytes], None] | None = None,
        on_tts_start: Callable[[dict], None] | None = None,
        on_tts_end: Callable[[dict], None] | None = None,
        on_chat_response: Callable[[str, str, str], None] | None = None,
        on_chat_ended: Callable[[str, str], None] | None = None,
        on_barge_in: Callable[[str], None] | None = None,
        on_session_started: Callable[[str], None] | None = None,
        on_error: Callable[[str], None] | None = None,
    ):
        self.app_id = app_id
        self.access_key = access_key
        self.on_asr = on_asr
        self.on_asr_ended = on_asr_ended
        self.on_tts_audio = on_tts_audio
        self.on_tts_start = on_tts_start
        self.on_tts_end = on_tts_end
        self.on_chat_response = on_chat_response
        self.on_chat_ended = on_chat_ended
        self.on_barge_in = on_barge_in
        self.on_session_started = on_session_started
        self.on_error = on_error
        self._ws: websockets.WebSocketClientProtocol | None = None
        self._session_id: str | None = None
        self._recv_task: asyncio.Task | None = None
        self._closed = False
        self._last_final_asr = ""

    def _ws_is_closed(self) -> bool:
        """Compatibility: websockets<=?? had .closed; websockets>=16 uses .state."""
        if not self._ws:
            return True
        state = getattr(self._ws, "state", None)
        if state is not None:
            try:
                return state == State.CLOSED
            except Exception:
                pass
        close_code = getattr(self._ws, "close_code", None)
        if close_code is not None:
            return True
        closed_attr = getattr(self._ws, "closed", None)
        if isinstance(closed_attr, bool):
            return closed_attr
        return False

    async def connect(self) -> None:
        headers = {
            "X-Api-App-ID": self.app_id,
            "X-Api-Access-Key": self.access_key,
            "X-Api-Resource-Id": "volc.speech.dialog",
            "X-Api-App-Key": "PlgvMymc7f3tQnJ6",
            "X-Api-Connect-Id": str(uuid.uuid4()),
        }
        self._ws = await websockets.connect(
            WS_URL,
            additional_headers=headers,
            ping_interval=20,
            ping_timeout=10,
            close_timeout=5,
        )
        self._closed = False
        frame = encode_client_json_event(EVENT_START_CONNECTION, {}, None)
        await self._ws.send(frame)
        LOG.info("Connected and sent StartConnection")

    async def start_session(
        self,
        system_role: str,
        speaking_style: str = "",
        bot_name: str = "背调访谈员",
        dialog_id: str = "",
        *,
        input_mod: str = "audio",
    ) -> str:
        if not self._ws or self._ws_is_closed():
            raise RuntimeError("Not connected")
        session_id = new_session_id()
        base = DEFAULT_START_SESSION_PAYLOAD
        # 深拷贝 dialog.extra 里的可变字段，覆盖 input_mod。
        dialog_extra = {**base["dialog"]["extra"], "input_mod": input_mod}
        dialog = {
            **base["dialog"],
            "bot_name": bot_name,
            "system_role": system_role,
            "speaking_style": speaking_style or base["dialog"]["speaking_style"],
            "dialog_id": dialog_id,
            "extra": dialog_extra,
        }
        payload = {**base, "dialog": dialog}
        frame = encode_client_json_event(EVENT_START_SESSION, payload, session_id)
        await self._ws.send(frame)
        self._session_id = session_id
        return session_id

    async def send_audio(self, pcm_chunk: bytes) -> None:
        if not self._ws or self._ws_is_closed() or not self._session_id:
            return
        frame = encode_audio_request(pcm_chunk, self._session_id)
        await self._ws.send(frame)

    async def send_text_query(self, text: str) -> None:
        if not self._ws or self._ws_is_closed() or not self._session_id:
            return
        frame = encode_client_json_event(
            EVENT_CHAT_TEXT_QUERY,
            {"content": text},
            self._session_id,
        )
        await self._ws.send(frame)

    async def conversation_update(self, *, item_id: str, text: str) -> None:
        """Send ConversationUpdate (511) to update a context item within the same session."""
        if not self._ws or self._ws_is_closed() or not self._session_id:
            return
        if not item_id:
            return
        frame = encode_client_json_event(
            511,
            {"items": [{"item_id": item_id, "text": text}]},
            self._session_id,
        )
        await self._ws.send(frame)

    async def conversation_create(self, *, role: str, text: str, timestamp: int | None = None) -> None:
        """Send ConversationCreate (510) to append a context item within the same session."""
        if not self._ws or self._ws_is_closed() or not self._session_id:
            return
        # Server enforces items length to be even (QA pairs). Send exactly 2 items every time.
        primary: dict[str, Any] = {"role": role, "text": text}
        if timestamp is not None:
            primary["timestamp"] = timestamp

        if role == "user":
            items = [primary, {"role": "assistant", "text": "（已记录）"}]
        else:
            items = [{"role": "user", "text": "（上下文更新）"}, primary]

        LOG.info("ConversationCreate(510) items_len=%s roles=%s", len(items), [i.get("role") for i in items])
        frame = encode_client_json_event(
            510,
            {"items": items},
            self._session_id,
        )
        await self._ws.send(frame)

    async def finish_session(self) -> None:
        if not self._ws or self._ws_is_closed() or not self._session_id:
            return
        frame = encode_client_json_event(EVENT_FINISH_SESSION, {}, self._session_id)
        await self._ws.send(frame)
        self._session_id = None

    async def disconnect(self) -> None:
        self._closed = True
        if self._recv_task and not self._recv_task.done():
            self._recv_task.cancel()
            try:
                await self._recv_task
            except asyncio.CancelledError:
                pass
        if self._ws and not self._ws_is_closed():
            try:
                if self._session_id:
                    await self.finish_session()
                frame = encode_client_json_event(EVENT_FINISH_CONNECTION, {}, None)
                await self._ws.send(frame)
            except Exception:
                pass
            await self._ws.close()
        self._ws = None
        self._session_id = None

    def _dispatch(self, event_id: int, payload: Any) -> None:
        if event_id == EVENT_SESSION_STARTED:
            if isinstance(payload, dict) and self.on_session_started:
                self.on_session_started(payload.get("dialog_id", ""))
        elif event_id == EVENT_SESSION_FAILED:
            if isinstance(payload, dict) and self.on_error:
                self.on_error(payload.get("error", "Session failed"))
        elif event_id == EVENT_ASR_INFO:
            if isinstance(payload, dict) and self.on_barge_in:
                self.on_barge_in(payload.get("question_id", ""))
        elif event_id == EVENT_ASR_RESPONSE:
            if isinstance(payload, dict) and self.on_asr:
                results = payload.get("results") or []
                for r in results:
                    if isinstance(r, dict):
                        text = r.get("text", "")
                        interim = r.get("is_interim", False)
                        self.on_asr(text, interim)
                        if not interim:
                            self._last_final_asr = text
        elif event_id == EVENT_ASR_ENDED:
            # Always notify ASR ended when event arrives; final text may be empty depending
            # on client-side buffering / interim behavior.
            if self.on_asr_ended:
                self.on_asr_ended(self._last_final_asr)
            self._last_final_asr = ""
        elif event_id == EVENT_TTS_SENTENCE_START:
            if isinstance(payload, dict) and self.on_tts_start:
                self.on_tts_start(payload)
        elif event_id == EVENT_TTS_RESPONSE:
            if isinstance(payload, bytes) and self.on_tts_audio:
                self.on_tts_audio(payload)
        elif event_id == EVENT_TTS_ENDED:
            if isinstance(payload, dict) and self.on_tts_end:
                self.on_tts_end(payload)
        elif event_id == EVENT_CHAT_RESPONSE:
            if isinstance(payload, dict) and self.on_chat_response:
                self.on_chat_response(
                    payload.get("content", ""),
                    payload.get("question_id", ""),
                    payload.get("reply_id", ""),
                )
        elif event_id == EVENT_CHAT_ENDED:
            if isinstance(payload, dict) and self.on_chat_ended:
                self.on_chat_ended(
                    payload.get("question_id", ""),
                    payload.get("reply_id", ""),
                )
        elif event_id == EVENT_DIALOG_COMMON_ERROR:
            if isinstance(payload, dict) and self.on_error:
                self.on_error(
                    f"{payload.get('status_code', '')} {payload.get('message', '')}"
                )

    async def _receive_loop(self) -> None:
        try:
            while self._ws and not self._ws_is_closed() and not self._closed:
                raw = await self._ws.recv()
                if isinstance(raw, str):
                    continue
                for event_id, payload in decode_server_frame(raw):
                    self._dispatch(event_id, payload)
        except ConnectionClosed:
            LOG.info("WebSocket connection closed")
        except asyncio.CancelledError:
            pass
        except Exception as e:
            LOG.exception("Receive loop error: %s", e)
            if self.on_error:
                self.on_error(str(e))

    def start_receive_loop(self) -> None:
        if self._recv_task and not self._recv_task.done():
            return
        self._recv_task = asyncio.create_task(self._receive_loop())
