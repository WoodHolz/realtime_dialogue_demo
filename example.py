#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Standalone Doubao T2S demo (single file, direct API call, no local backend).

Features:
- Build binary WS protocol frames in-file
- StartConnection -> StartSession(input_mod=text)
- Send ChatTextQuery(501)
- Receive streamed TTS PCM (24k, mono, s16le) and play immediately

Install:
  pip install websockets sounddevice

Run:
  export DOUBAO_APP_ID="your_app_id"
  export DOUBAO_ACCESS_KEY="your_access_key"
  python example.py
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import queue
import struct
import threading
import uuid
from typing import Any

import sounddevice as sd
import websockets

from config.prompt_templates import DEFAULT_SPEAKING_STYLE, DEFAULT_SYSTEM_ROLE

WS_URL = "wss://openspeech.bytedance.com/api/v3/realtime/dialogue"
RESOURCE_ID = "volc.speech.dialog"
APP_KEY = "PlgvMymc7f3tQnJ6"
TTS_SAMPLE_RATE = 24000

# Protocol constants
PROTOCOL_VERSION = 0x01
HEADER_SIZE = 0x01

MSG_CLIENT_TEXT = 0x01
MSG_AUDIO_REQUEST = 0x02

SER_RAW = 0x00
SER_JSON = 0x01
COMPRESS_NONE = 0x00

FLAG_HAS_EVENT = 0x04

EVENT_START_CONNECTION = 1
EVENT_FINISH_CONNECTION = 2
EVENT_START_SESSION = 100
EVENT_FINISH_SESSION = 102
EVENT_CHAT_TEXT_QUERY = 501

EVENT_CONNECTION_STARTED = 50
EVENT_SESSION_STARTED = 150
EVENT_SESSION_FAILED = 153
EVENT_TTS_RESPONSE = 352
EVENT_TTS_ENDED = 359
EVENT_CHAT_RESPONSE = 550
EVENT_CHAT_ENDED = 559
EVENT_DIALOG_COMMON_ERROR = 599


def _pack_header(message_type: int, serialization: int, flags: int = FLAG_HAS_EVENT) -> bytes:
    b0 = (PROTOCOL_VERSION << 4) | HEADER_SIZE
    b1 = (message_type << 4) | flags
    b2 = (serialization << 4) | COMPRESS_NONE
    b3 = 0x00
    return struct.pack(">BBBB", b0, b1, b2, b3)


def _pack_optional_session(session_id: str | None) -> bytes:
    if not session_id:
        return b""
    sid = session_id.encode("utf-8")
    return struct.pack(">I", len(sid)) + sid


def encode_client_json_event(event_id: int, payload_obj: dict[str, Any], session_id: str | None) -> bytes:
    payload = json.dumps(payload_obj, ensure_ascii=False).encode("utf-8")
    header = _pack_header(MSG_CLIENT_TEXT, SER_JSON, FLAG_HAS_EVENT)
    optional = struct.pack(">I", event_id) + _pack_optional_session(session_id) + struct.pack(">I", len(payload))
    return header + optional + payload


def decode_server_frame(data: bytes) -> list[tuple[int, dict[str, Any] | bytes]]:
    """
    Decode Doubao server frame(s) from one ws packet.
    Returns: list[(event_id, payload)], payload is dict(json) or bytes(raw).
    """
    out: list[tuple[int, dict[str, Any] | bytes]] = []
    i = 0
    n = len(data)
    while i + 4 <= n:
        _, b1, b2, _ = struct.unpack(">BBBB", data[i : i + 4])
        i += 4
        _msg_type = (b1 >> 4) & 0x0F
        flags = b1 & 0x0F
        serialization = (b2 >> 4) & 0x0F

        event_id = 0
        if flags & FLAG_HAS_EVENT:
            if i + 4 > n:
                break
            (event_id,) = struct.unpack(">I", data[i : i + 4])
            i += 4

        # Connection-level events usually have no session id.
        if event_id not in (50, 51, 52):
            if i + 4 > n:
                break
            (sid_len,) = struct.unpack(">I", data[i : i + 4])
            i += 4
            if sid_len > 0:
                if i + sid_len > n:
                    break
                i += sid_len

        if i + 4 > n:
            break
        (payload_len,) = struct.unpack(">I", data[i : i + 4])
        i += 4
        if i + payload_len > n:
            break
        payload = data[i : i + payload_len]
        i += payload_len

        if serialization == SER_JSON and payload:
            try:
                payload = json.loads(payload.decode("utf-8"))
            except Exception:
                pass
        out.append((event_id, payload))
    return out


class AudioPlayer:
    def __init__(self, sample_rate: int = TTS_SAMPLE_RATE):
        self.sample_rate = sample_rate
        self.q: "queue.Queue[bytes]" = queue.Queue(maxsize=200)
        self.stop_event = threading.Event()
        self.worker = threading.Thread(target=self._run, daemon=True)
        self.worker.start()

    def _run(self) -> None:
        with sd.RawOutputStream(
            samplerate=self.sample_rate,
            channels=1,
            dtype="int16",
            blocksize=0,
        ) as stream:
            while not self.stop_event.is_set():
                try:
                    chunk = self.q.get(timeout=0.1)
                except queue.Empty:
                    continue
                if chunk:
                    stream.write(chunk)

    def feed(self, pcm_bytes: bytes) -> None:
        try:
            self.q.put_nowait(pcm_bytes)
        except queue.Full:
            # If playback falls behind, drop oldest latency-sensitive chunk.
            try:
                _ = self.q.get_nowait()
            except queue.Empty:
                pass
            try:
                self.q.put_nowait(pcm_bytes)
            except queue.Full:
                pass

    def close(self) -> None:
        self.stop_event.set()
        self.worker.join(timeout=1.0)


async def run_t2s(app_id: str, access_key: str, speaker: str, system_role: str) -> None:
    headers = {
        "X-Api-App-ID": app_id,
        "X-Api-Access-Key": access_key,
        "X-Api-Resource-Id": RESOURCE_ID,
        "X-Api-App-Key": APP_KEY,
        "X-Api-Connect-Id": str(uuid.uuid4()),
    }

    player = AudioPlayer(sample_rate=TTS_SAMPLE_RATE)
    current_reply_id = ""

    async with websockets.connect(
        WS_URL,
        additional_headers=headers,
        ping_interval=20,
        ping_timeout=10,
    ) as ws:
        # 1) StartConnection
        await ws.send(encode_client_json_event(EVENT_START_CONNECTION, {}, None))

        # 2) StartSession(input_mod=text)
        session_id = str(uuid.uuid4())
        start_payload = {
            "asr": {
                "audio_info": {"format": "speech_pcm", "sample_rate": 16000, "channel": 1},
                "extra": {"end_smooth_window_ms": 1500},
            },
            "tts": {
                "speaker": speaker,
                "audio_config": {"channel": 1, "format": "pcm_s16le", "sample_rate": 24000},
            },
            "dialog": {
                "bot_name": "背调访谈员",
                "system_role": system_role,
                "speaking_style": DEFAULT_SPEAKING_STYLE,
                "dialog_id": "",
                "extra": {
                    "model": "1.2.1.1",
                    "input_mod": "text",
                    "strict_audit": False,
                    "audit_response": "当前为测试环境，如触发安全审核，请联系管理员。",
                    "recv_timeout": 10,
                },
            },
        }
        await ws.send(encode_client_json_event(EVENT_START_SESSION, start_payload, session_id))
        print("Connected. Session starting...")

        async def receiver() -> None:
            nonlocal current_reply_id
            async for raw in ws:
                if isinstance(raw, str):
                    continue
                events = decode_server_frame(raw)
                for event_id, payload in events:
                    if event_id == EVENT_CONNECTION_STARTED:
                        print("[event] connection started")
                    elif event_id == EVENT_SESSION_STARTED:
                        print("[event] session started")
                    elif event_id == EVENT_SESSION_FAILED:
                        print(f"[event] session failed: {payload}")
                    elif event_id == EVENT_CHAT_RESPONSE and isinstance(payload, dict):
                        rid = payload.get("reply_id", "")
                        content = payload.get("content", "")
                        if rid != current_reply_id:
                            current_reply_id = rid
                            print("\nAI: ", end="", flush=True)
                        print(content, end="", flush=True)
                    elif event_id == EVENT_CHAT_ENDED:
                        current_reply_id = ""
                        print("")
                    elif event_id == EVENT_TTS_RESPONSE and isinstance(payload, (bytes, bytearray)):
                        player.feed(bytes(payload))
                    elif event_id == EVENT_TTS_ENDED:
                        pass
                    elif event_id == EVENT_DIALOG_COMMON_ERROR:
                        print(f"\n[event] dialog error: {payload}")

        recv_task = asyncio.create_task(receiver())

        try:
            print("Type your text. Ctrl+C to quit.")
            while True:
                text = await asyncio.to_thread(input, "You: ")
                text = (text or "").strip()
                if not text:
                    continue
                print(f"你: {text}")
                await ws.send(
                    encode_client_json_event(
                        EVENT_CHAT_TEXT_QUERY,
                        {"content": text},
                        session_id,
                    )
                )
        except (KeyboardInterrupt, EOFError):
            print("\nStopping...")
        finally:
            try:
                await ws.send(encode_client_json_event(EVENT_FINISH_SESSION, {}, session_id))
            except Exception:
                pass
            try:
                await ws.send(encode_client_json_event(EVENT_FINISH_CONNECTION, {}, None))
            except Exception:
                pass
            recv_task.cancel()
            with contextlib.suppress(Exception):
                await recv_task
            player.close()


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Standalone Doubao Realtime T2S demo")
    p.add_argument("--app-id", default=os.getenv("DOUBAO_APP_ID", ""), help="Doubao app id")
    p.add_argument("--access-key", default=os.getenv("DOUBAO_ACCESS_KEY", ""), help="Doubao access key")
    p.add_argument(
        "--speaker",
        default="zh_male_xiaotian_jupiter_bigtts",
        help="TTS speaker id",
    )
    p.add_argument(
        "--system-role",
        default=DEFAULT_SYSTEM_ROLE,
        help="dialog.system_role",
    )
    return p.parse_args()


if __name__ == "__main__":
    import contextlib

    args = parse_args()
    if not args.app_id or not args.access_key:
        raise SystemExit(
            "Missing credentials. Set DOUBAO_APP_ID / DOUBAO_ACCESS_KEY, "
            "or pass --app-id / --access-key."
        )
    asyncio.run(
        run_t2s(
            app_id=args.app_id,
            access_key=args.access_key,
            speaker=args.speaker,
            system_role=args.system_role,
        )
    )