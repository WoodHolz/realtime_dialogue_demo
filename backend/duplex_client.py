# -*- coding: utf-8 -*-
"""
Doubao Realtime Voice API 3.0 (Seeduplex) — full-duplex JSON WebSocket client.

Protocol: pure JSON text frames over wss://openspeech.bytedance.com/api/v3/duplex/realtime/dialogue
Auth:
- New console: X-Api-Key (DOUBAO_API_KEY)
- Legacy console: X-Api-App-ID + X-Api-Access-Key (DOUBAO_APP_ID + DOUBAO_ACCESS_KEY)
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import uuid
from typing import Any, Callable

import websockets
from websockets.exceptions import ConnectionClosed
from websockets.protocol import State

from backend.audio_stream import CHUNK_BYTES, pcm_chunks_from_bytes
from config.prompt_templates import DEFAULT_SPEAKING_STYLE, DEFAULT_SYSTEM_ROLE

LOG = logging.getLogger(__name__)

WS_URL = "wss://openspeech.bytedance.com/api/v3/duplex/realtime/dialogue"
DUPLEX_MODEL = "1.2.6.0"
DUPLEX_SILENCE_CHUNK = bytes(CHUNK_BYTES)
DEFAULT_SPEAKER = "zh_male_xiaotian_jupiter_bigtts"
# urge_auth / tmp_prod outbound SIP 默认音色（豆包 Jupiter bigtts）
OUTBOUND_SPEAKER = "zh_female_vv_jupiter_bigtts"
LEGACY_RESOURCE_ID = "volc.speech.dialog"
LEGACY_APP_KEY = "PlgvMymc7f3tQnJ6"

TYPE_SESSION_CREATE = "session.create"
TYPE_SESSION_UPDATE = "session.update"
TYPE_SESSION_CLOSE = "session.close"
TYPE_INPUT_AUDIO_BUFFER_APPEND = "input_audio_buffer.append"
TYPE_INPUT_AUDIO_BUFFER_COMMIT = "input_audio_buffer.commit"
TYPE_CONVERSATION_ITEM_CREATE = "conversation.item.create"
TYPE_CONVERSATION_ITEM_UPDATE = "conversation.item.update"
TYPE_RESPONSE_CANCEL = "response.cancel"
# Binary SayHello (event 300, payload {"content": "..."}) maps to this JSON event with `text`.
TYPE_SPEECH_TEXT_BUFFER_COMMIT = "speech_text_buffer.commit"

TYPE_SESSION_CREATED = "session.created"
TYPE_SESSION_CLOSED = "session.closed"
TYPE_TRANSCRIPTION_STARTED = "conversation.item.input_audio_transcription.started"
TYPE_TRANSCRIPTION_DELTA = "conversation.item.input_audio_transcription.delta"
TYPE_TRANSCRIPTION_COMPLETED = "conversation.item.input_audio_transcription.completed"
TYPE_TRANSCRIPTION_FAILED = "conversation.item.input_audio_transcription.failed"
TYPE_RESPONSE_OUTPUT_TEXT_DELTA = "response.output_text.delta"
TYPE_RESPONSE_OUTPUT_TEXT_DONE = "response.output_text.done"
TYPE_RESPONSE_OUTPUT_AUDIO_STARTED = "response.output_audio.started"
TYPE_RESPONSE_OUTPUT_AUDIO_DELTA = "response.output_audio.delta"
TYPE_RESPONSE_OUTPUT_AUDIO_DONE = "response.output_audio.done"
TYPE_RESPONSE_DONE = "response.done"
TYPE_RESPONSE_CANCELED = "response.canceled"
TYPE_ERROR = "error"

DEFAULT_EXTENSION = {
    "asr": {
        "audio_info": {
            "format": "speech_pcm",
            "sample_rate": 16000,
            "channel": 1,
        },
        "extra": {
            "end_smooth_window_ms": 1500,
        },
    },
    "tts": {
        "speaker": DEFAULT_SPEAKER,
        "audio_config": {
            "channel": 1,
            "format": "pcm_s16le",
            "sample_rate": 24000,
        },
    },
    "dialog": {
        "extra": {
            "strict_audit": False,
            "audit_response": "当前为测试环境，如触发安全审核，请联系管理员。",
            "recv_timeout": 10,
        },
    },
}


class DuplexVoiceClient:
    """Async client for Doubao Realtime Voice API 3.0 (full-duplex JSON protocol)."""

    def __init__(
        self,
        *,
        api_key: str = "",
        app_id: str = "",
        access_key: str = "",
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
        api_key = (api_key or "").strip()
        app_id = (app_id or "").strip()
        access_key = (access_key or "").strip()
        if api_key:
            self._auth_mode = "api_key"
            self.api_key = api_key
            self.app_id = ""
            self.access_key = ""
        elif app_id and access_key:
            self._auth_mode = "legacy"
            self.api_key = ""
            self.app_id = app_id
            self.access_key = access_key
        else:
            raise ValueError(
                "DuplexVoiceClient requires DOUBAO_API_KEY (new console) "
                "or DOUBAO_APP_ID + DOUBAO_ACCESS_KEY (legacy console)"
            )
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
        self._dialog_id: str = ""
        self._recv_task: asyncio.Task | None = None
        self._closed = False
        self._event_id = 0
        self._write_lock = asyncio.Lock()
        self._last_final_asr = ""
        self._current_response_id = ""
        self._current_item_id = ""
        self._chat_text_buffer = ""
        self._input_mod = "audio"
        self._server_vad = False
        self._silence_keepalive_task: asyncio.Task | None = None
        self._user_audio_sent = False
        self._session_system_role = ""
        self._session_speaking_style = ""
        self._session_bot_name = ""
        self._session_dialog_id = ""
        self._session_voice = DEFAULT_SPEAKER
        self._session_end_smooth_window_ms = 1500
        self._session_recv_timeout = 10

    def _new_event_id(self) -> str:
        self._event_id += 1
        return f"event_{self._event_id}"

    def _ws_is_closed(self) -> bool:
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

    def auth_mode(self) -> str:
        return self._auth_mode

    def _message_item(self, role: str, text: str) -> dict[str, Any]:
        return {
            "type": "message",
            "role": role,
            "content": [{"type": "input_text", "text": text}],
        }

    def _qa_pair_items(self, user_text: str, assistant_text: str = "（已记录）") -> list[dict[str, Any]]:
        return [
            self._message_item("user", user_text),
            self._message_item("assistant", assistant_text),
        ]

    async def start_silence_keepalive(self, interval_sec: float = 0.02) -> None:
        """Official demo sends silence when no mic/file audio to avoid idle timeout."""
        await self.stop_silence_keepalive()

        async def _loop() -> None:
            try:
                while self._session_id and not self._closed and not self._ws_is_closed():
                    await self.send_audio(DUPLEX_SILENCE_CHUNK)
                    await asyncio.sleep(interval_sec)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                LOG.debug("duplex silence keepalive stopped: %s", exc)

        self._silence_keepalive_task = asyncio.create_task(_loop())

    async def stop_silence_keepalive(self) -> None:
        task = self._silence_keepalive_task
        self._silence_keepalive_task = None
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    def _connect_headers(self) -> dict[str, str]:
        if self._auth_mode == "api_key":
            return {"X-Api-Key": self.api_key}
        return {
            "X-Api-App-ID": self.app_id,
            "X-Api-Access-Key": self.access_key,
            "X-Api-Resource-Id": LEGACY_RESOURCE_ID,
            "X-Api-App-Key": LEGACY_APP_KEY,
            "X-Api-Connect-Id": str(uuid.uuid4()),
        }

    async def connect(self) -> None:
        headers = self._connect_headers()
        self._ws = await websockets.connect(
            WS_URL,
            additional_headers=headers,
            ping_interval=20,
            ping_timeout=10,
            close_timeout=5,
        )
        self._closed = False
        logid = ""
        try:
            resp_headers = getattr(self._ws, "response_headers", None)
            if resp_headers:
                logid = resp_headers.get("X-Tt-Logid", "")
        except Exception:
            pass
        LOG.info("Duplex 3.0 connected auth=%s (logid=%s)", self._auth_mode, logid or "n/a")

    def _build_extension(
        self,
        *,
        system_role: str,
        speaking_style: str,
        bot_name: str,
        dialog_id: str,
        input_mod: str,
        end_smooth_window_ms: int = 1500,
        server_vad: bool = False,
        recv_timeout: int = 10,
        voice: str = "",
    ) -> dict[str, Any]:
        base = DEFAULT_EXTENSION
        speaker = (voice or DEFAULT_SPEAKER).strip() or DEFAULT_SPEAKER
        asr_extra = {
            **base["asr"]["extra"],
            "end_smooth_window_ms": end_smooth_window_ms,
        }
        if server_vad:
            # Duplex 3.0 server-side endpointing (semantic VAD); disable client custom VAD.
            asr_extra["enable_custom_vad"] = False
        dialog_extra = {
            **base["dialog"]["extra"],
            "input_mod": input_mod,
            "recv_timeout": int(recv_timeout),
        }
        return {
            "asr": {**base["asr"], "extra": asr_extra},
            "tts": {**base["tts"], "speaker": speaker},
            "dialog": {
                **base["dialog"],
                "bot_name": bot_name,
                "system_role": system_role,
                "speaking_style": speaking_style,
                "dialog_id": dialog_id,
                "extra": dialog_extra,
            },
        }

    async def _send_event(self, event: dict[str, Any]) -> None:
        if not self._ws or self._ws_is_closed():
            return
        payload = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
        async with self._write_lock:
            await self._ws.send(payload)

    async def start_session(
        self,
        system_role: str,
        speaking_style: str = "",
        bot_name: str = "背调访谈员",
        dialog_id: str = "",
        *,
        input_mod: str = "audio",
        server_vad: bool = False,
        end_smooth_window_ms: int = 1500,
        recv_timeout: int = 10,
        voice: str = "",
    ) -> str:
        if not self._ws or self._ws_is_closed():
            raise RuntimeError("Not connected")
        session_id = dialog_id or str(uuid.uuid4())
        style = speaking_style or DEFAULT_SPEAKING_STYLE
        role = system_role or DEFAULT_SYSTEM_ROLE
        speaker = (voice or DEFAULT_SPEAKER).strip() or DEFAULT_SPEAKER
        # Align with official demo: instructions only on session; dialog fields live in extension.
        instructions = role
        if style:
            instructions = f"{instructions}\n\n【说话风格】\n{style}"
        if server_vad:
            resolved_input_mod = "audio"
        elif input_mod == "text":
            # Local ASR + text query path: keep duplex alive with silence between turns.
            resolved_input_mod = "keep_alive"
        else:
            resolved_input_mod = input_mod
        self._input_mod = resolved_input_mod
        self._server_vad = server_vad
        extension = self._build_extension(
            system_role=role,
            speaking_style=style,
            bot_name=bot_name,
            dialog_id=dialog_id,
            input_mod=resolved_input_mod,
            end_smooth_window_ms=end_smooth_window_ms,
            server_vad=server_vad,
            recv_timeout=recv_timeout,
            voice=speaker,
        )
        session = {
            "id": session_id,
            "model": DUPLEX_MODEL,
            "instructions": instructions,
            "audio": {
                "input": {"format": {"type": "pcm", "rate": 16000}},
                "output": {
                    "format": {"type": "pcm_s16le", "rate": 24000},
                    "voice": speaker,
                },
            },
        }
        await self._send_event(
            {
                "type": TYPE_SESSION_CREATE,
                "event_id": self._new_event_id(),
                "session": session,
                "extension": extension,
            }
        )
        self._session_id = session_id
        self._user_audio_sent = False
        self._session_system_role = role
        self._session_speaking_style = style
        self._session_bot_name = bot_name
        self._session_dialog_id = dialog_id or session_id
        self._session_voice = speaker
        self._session_end_smooth_window_ms = end_smooth_window_ms
        self._session_recv_timeout = recv_timeout
        await self.start_silence_keepalive()
        return session_id

    async def update_session(
        self,
        *,
        input_mod: str = "audio",
        server_vad: bool = False,
        end_smooth_window_ms: int | None = None,
        recv_timeout: int | None = None,
    ) -> None:
        """Update duplex dialog extension (e.g. keep_alive → audio after opening)."""
        if not self._ws or self._ws_is_closed() or not self._session_id:
            return
        if server_vad:
            resolved_input_mod = "audio"
        elif input_mod == "text":
            resolved_input_mod = "keep_alive"
        else:
            resolved_input_mod = input_mod
        self._input_mod = resolved_input_mod
        self._server_vad = server_vad
        smooth_ms = (
            end_smooth_window_ms
            if end_smooth_window_ms is not None
            else self._session_end_smooth_window_ms
        )
        timeout = recv_timeout if recv_timeout is not None else self._session_recv_timeout
        extension = self._build_extension(
            system_role=self._session_system_role,
            speaking_style=self._session_speaking_style,
            bot_name=self._session_bot_name,
            dialog_id=self._session_dialog_id,
            voice=self._session_voice,
            input_mod=resolved_input_mod,
            end_smooth_window_ms=smooth_ms,
            server_vad=server_vad,
            recv_timeout=timeout,
        )
        await self._send_event(
            {
                "type": TYPE_SESSION_UPDATE,
                "event_id": self._new_event_id(),
                "session": {"id": self._session_id},
                "extension": extension,
            }
        )
        LOG.info(
            "Duplex session.update input_mod=%s server_vad=%s recv_timeout=%s",
            resolved_input_mod,
            server_vad,
            timeout,
        )

    async def send_audio(self, pcm_chunk: bytes) -> None:
        if not self._ws or self._ws_is_closed() or not self._session_id:
            return
        if pcm_chunk.strip(b"\x00"):
            self._user_audio_sent = True
            await self.stop_silence_keepalive()
        await self._send_event(
            {
                "type": TYPE_INPUT_AUDIO_BUFFER_APPEND,
                "event_id": self._new_event_id(),
                "audio": base64.b64encode(pcm_chunk).decode("ascii"),
            }
        )

    async def send_audio_turn(self, pcm_s16le_16k: bytes) -> None:
        """Stream one user utterance then commit, matching official duplex audio flow."""
        if not pcm_s16le_16k:
            return
        await self.stop_silence_keepalive()
        for chunk in pcm_chunks_from_bytes(pcm_s16le_16k):
            await self.send_audio(chunk)
        await self._send_event(
            {
                "type": TYPE_INPUT_AUDIO_BUFFER_COMMIT,
                "event_id": self._new_event_id(),
            }
        )

    async def send_text_query(self, text: str) -> None:
        """Inject user text as a QA pair (items length must be even)."""
        if not self._ws or self._ws_is_closed() or not self._session_id:
            return
        text = (text or "").strip()
        if not text:
            return
        await self._send_event(
            {
                "type": TYPE_CONVERSATION_ITEM_CREATE,
                "event_id": self._new_event_id(),
                "items": self._qa_pair_items(text),
            }
        )

    async def say_hello(self, content: str) -> None:
        """Proactive opening TTS (SayHello / event 300).

        Binary API: event 300, payload ``{"content": "..."}``.
        Duplex 3.0 JSON wire: ``speech_text_buffer.commit`` with ``text``.
        """
        if not self._ws or self._ws_is_closed() or not self._session_id:
            return
        text = (content or "").strip().strip("「」").strip()
        if not text:
            return
        await self._send_event(
            {
                "type": TYPE_SPEECH_TEXT_BUFFER_COMMIT,
                "event_id": self._new_event_id(),
                "text": text,
            }
        )
        LOG.info("SayHello sent (speech_text_buffer.commit) len=%d", len(text))

    async def speak_opening_script(self, opening_line: str) -> None:
        """Alias for SIP opening; sends SayHello with exact opening script."""
        await self.say_hello(opening_line)

    async def conversation_update(self, *, item_id: str, text: str) -> None:
        if not self._ws or self._ws_is_closed() or not self._session_id:
            return
        if not item_id:
            return
        await self._send_event(
            {
                "type": TYPE_CONVERSATION_ITEM_UPDATE,
                "event_id": self._new_event_id(),
                "items": [{"item_id": item_id, "text": text}],
            }
        )

    async def conversation_create(self, *, role: str, text: str, timestamp: int | None = None) -> None:
        if not self._ws or self._ws_is_closed() or not self._session_id:
            return
        primary = self._message_item(role, text)
        if timestamp is not None:
            primary["timestamp"] = timestamp
        if role == "user":
            items = [primary, self._message_item("assistant", "（已记录）")]
        else:
            items = [self._message_item("user", "（上下文更新）"), primary]
        LOG.info(
            "conversation.item.create items_len=%s roles=%s",
            len(items),
            [i.get("role") for i in items],
        )
        await self._send_event(
            {
                "type": TYPE_CONVERSATION_ITEM_CREATE,
                "event_id": self._new_event_id(),
                "items": items,
            }
        )

    async def finish_session(self) -> None:
        if not self._ws or self._ws_is_closed() or not self._session_id:
            return
        await self.stop_silence_keepalive()
        await self._send_event({"type": TYPE_SESSION_CLOSE, "event_id": self._new_event_id()})
        self._session_id = None

    async def disconnect(self) -> None:
        self._closed = True
        await self.stop_silence_keepalive()
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
                    await self._wait_session_closed(timeout=3.0)
            except Exception:
                pass
            await self._ws.close()
        self._ws = None
        self._session_id = None

    async def _wait_session_closed(self, timeout: float = 3.0) -> None:
        if not self._ws or self._ws_is_closed():
            return
        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            try:
                remaining = deadline - asyncio.get_event_loop().time()
                raw = await asyncio.wait_for(self._ws.recv(), timeout=remaining)
                if isinstance(raw, bytes):
                    raw = raw.decode("utf-8")
                event = json.loads(raw)
                self._dispatch(event)
                if event.get("type") == TYPE_SESSION_CLOSED:
                    return
            except asyncio.TimeoutError:
                return
            except Exception:
                return

    def _dispatch(self, event: dict[str, Any]) -> None:
        event_type = event.get("type", "")

        if event_type == TYPE_SESSION_CREATED:
            session = event.get("session") or {}
            self._dialog_id = session.get("id", "") or self._dialog_id
            if self.on_session_started:
                self.on_session_started(self._dialog_id)
        elif event_type == TYPE_TRANSCRIPTION_STARTED:
            item_id = event.get("item_id", "")
            self._current_item_id = item_id or self._current_item_id
            if self.on_barge_in:
                self.on_barge_in(item_id)
        elif event_type == TYPE_TRANSCRIPTION_DELTA:
            delta = event.get("delta", "")
            item_id = event.get("item_id", "")
            if item_id:
                self._current_item_id = item_id
            if delta and self.on_asr:
                self.on_asr(delta, True)
        elif event_type == TYPE_TRANSCRIPTION_COMPLETED:
            transcript = event.get("transcript") or event.get("text") or ""
            item_id = event.get("item_id", "")
            if item_id:
                self._current_item_id = item_id
            self._last_final_asr = transcript
            if self.on_asr and transcript:
                self.on_asr(transcript, False)
            if self.on_asr_ended:
                self.on_asr_ended(self._last_final_asr)
            self._last_final_asr = ""
        elif event_type == TYPE_TRANSCRIPTION_FAILED:
            if self.on_error:
                self.on_error(f"ASR failed: {event}")
        elif event_type == TYPE_RESPONSE_OUTPUT_AUDIO_STARTED:
            response_id = event.get("response_id", "")
            self._current_response_id = response_id or self._current_response_id
            if self.on_tts_start:
                self.on_tts_start(
                    {
                        "question_id": self._current_item_id,
                        "reply_id": response_id,
                        "tts_type": event.get("tts_type", ""),
                    }
                )
        elif event_type == TYPE_RESPONSE_OUTPUT_AUDIO_DELTA:
            delta_b64 = event.get("delta") or ""
            if delta_b64 and self.on_tts_audio:
                try:
                    self.on_tts_audio(base64.b64decode(delta_b64))
                except Exception as exc:
                    LOG.warning("TTS delta base64 decode failed: %s", exc)
        elif event_type == TYPE_RESPONSE_OUTPUT_AUDIO_DONE:
            if self.on_tts_end:
                self.on_tts_end(
                    {
                        "question_id": self._current_item_id,
                        "reply_id": event.get("response_id", self._current_response_id),
                        "status_code": event.get("status_code", ""),
                    }
                )
        elif event_type == TYPE_RESPONSE_OUTPUT_TEXT_DELTA:
            delta = event.get("delta", "")
            response_id = event.get("response_id", "")
            if response_id:
                self._current_response_id = response_id
            self._chat_text_buffer += delta
            if delta and self.on_chat_response:
                self.on_chat_response(
                    delta,
                    self._current_item_id,
                    response_id or self._current_response_id,
                )
        elif event_type == TYPE_RESPONSE_OUTPUT_TEXT_DONE:
            response_id = event.get("response_id", self._current_response_id)
            if self.on_chat_ended:
                self.on_chat_ended(self._current_item_id, response_id)
            self._chat_text_buffer = ""
        elif event_type == TYPE_ERROR:
            err = event.get("error")
            code = event.get("status_code") or event.get("code") or ""
            message = event.get("message") or ""
            if isinstance(err, dict):
                code = code or err.get("code", "") or err.get("status_code", "")
                message = message or err.get("message", "") or str(err)
            elif err:
                message = message or str(err)
            msg = f"{code} {message}".strip() or str(event)
            LOG.warning("Duplex error event: %s", msg)
            if self.on_error:
                self.on_error(msg)
        elif event_type == TYPE_SESSION_CLOSED:
            LOG.info("Duplex session closed")

    async def _receive_loop(self) -> None:
        try:
            while self._ws and not self._ws_is_closed() and not self._closed:
                raw = await self._ws.recv()
                if isinstance(raw, bytes):
                    raw = raw.decode("utf-8")
                try:
                    event = json.loads(raw)
                except json.JSONDecodeError as exc:
                    LOG.warning("Invalid JSON frame: %s", exc)
                    continue
                self._dispatch(event)
        except ConnectionClosed:
            LOG.info("Duplex WebSocket connection closed")
        except asyncio.CancelledError:
            pass
        except Exception as e:
            LOG.exception("Duplex receive loop error: %s", e)
            if self.on_error:
                self.on_error(str(e))

    def start_receive_loop(self) -> None:
        if self._recv_task and not self._recv_task.done():
            return
        self._recv_task = asyncio.create_task(self._receive_loop())
