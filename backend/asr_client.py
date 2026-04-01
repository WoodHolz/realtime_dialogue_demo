# -*- coding: utf-8 -*-
"""
豆包流式语音识别 (ASR) client for t2s mode.

对齐官方 sauc_python 示例：
- 鉴权：X-Api-App-Key、X-Api-Access-Key、X-Api-Resource-Id、X-Api-Request-Id
- 协议：GZIP 压缩、序列号（最后一包负 seq）
- 默认 Resource-Id：volc.bigasr.sauc.duration
- 接口：bigmodel_async 流式
"""
from __future__ import annotations

import asyncio
import gzip
import json
import logging
import struct
import uuid
from dataclasses import dataclass
from typing import Callable, Awaitable

import websockets
from websockets.exceptions import ConnectionClosed

LOG = logging.getLogger(__name__)

ASR_WS_URL = "wss://openspeech.bytedance.com/api/v3/sauc/bigmodel_async"

# 火山流式 ASR 常见错误码（文档/社区摘要，便于排障）
ASR_KNOWN_ERROR_HINTS: dict[int, str] = {
    45000081: " 等包超时/流式分包间隔过大或服务端等待音频超时",
}


def asr_error_hint(code: int) -> str:
    return ASR_KNOWN_ERROR_HINTS.get(code, "")

# 官方示例使用 volc.bigasr.sauc.duration；2.0 并发版为 volc.seedasr.sauc.concurrent
DEFAULT_ASR_RESOURCE_ID = "volc.bigasr.sauc.duration"

# 双向流式模式推荐 200ms 分包：16kHz s16le → 3200 samples × 2 = 6400 bytes
CHUNK_MS = 200
CHUNK_BYTES = (16000 * CHUNK_MS // 1000) * 2

def _asr_auth_headers(app_id: str, access_key: str, resource_id: str) -> dict:
    """鉴权头，对齐官方：X-Api-App-Key、X-Api-Access-Key、X-Api-Resource-Id、X-Api-Request-Id"""
    return {
        "X-Api-App-Key": app_id,
        "X-Api-Access-Key": access_key,
        "X-Api-Resource-Id": resource_id,
        "X-Api-Request-Id": str(uuid.uuid4()),
    }


def _parse_asr_response(raw: bytes) -> tuple[dict | None, bool, bool, int | None]:
    """解析 ASR 响应。返回 (payload_msg, is_last, is_error, error_code)。"""
    if len(raw) < 4:
        return None, False, False, None
    header_size = raw[0] & 0x0F
    msg_type = (raw[1] >> 4) & 0x0F
    flags = raw[1] & 0x0F
    msg_compression = raw[2] & 0x0F
    payload = raw[header_size * 4 :]
    if flags & 0x01 and len(payload) >= 4:
        payload = payload[4:]
    if flags & 0x04 and len(payload) >= 4:
        payload = payload[4:]
    if msg_type == 0b1111:
        err_code: int | None = None
        if len(payload) >= 4:
            err_code = struct.unpack(">i", payload[:4])[0]
            hint = asr_error_hint(err_code)
            LOG.warning("ASR server error code=%s%s", err_code, f" —{hint}" if hint else "")
        return None, False, True, err_code
    if msg_type != 0b1001 or len(payload) < 4:
        return None, False, False, None
    payload_size = struct.unpack(">I", payload[:4])[0]
    payload_bytes = payload[4 : 4 + payload_size]
    if msg_compression == 0b0001:
        try:
            payload_bytes = gzip.decompress(payload_bytes)
        except Exception:
            return None, False, False, None
    try:
        obj = json.loads(payload_bytes.decode("utf-8"))
    except Exception:
        return None, False, False, None
    is_last = bool(flags & 0x02)
    return obj, is_last, False, None


def _extract_text(obj: dict) -> str:
    """从 ASR 响应中提取文本，支持多种结构。"""
    result = obj.get("result")
    if isinstance(result, dict):
        text = (
            result.get("text")
            or result.get("voice_text_str")
            or result.get("voice_text")
        )
        if text:
            return str(text).strip()
    if isinstance(result, list) and result:
        for item in reversed(result):
            if isinstance(item, dict):
                text = (
                    item.get("text")
                    or item.get("voice_text_str")
                    or item.get("voice_text")
                )
                if text:
                    return str(text).strip()
    # utterances 分句结果
    utterances = obj.get("utterances")
    if isinstance(utterances, list) and utterances:
        parts = []
        for u in utterances:
            if isinstance(u, dict):
                t = u.get("text") or u.get("voice_text_str") or u.get("voice_text")
                if t:
                    parts.append(str(t).strip())
        if parts:
            return " ".join(parts)
    return ""


def _build_header(*, message_type: int, flags: int, serialization: int, compression: int) -> bytes:
    """
    Build 4-byte header.
    - version: 0b0001
    - header size: 0b0001 (4 bytes)
    """
    version = 0b0001
    header_size = 0b0001
    b0 = (version << 4) | header_size
    b1 = ((message_type & 0x0F) << 4) | (flags & 0x0F)
    b2 = ((serialization & 0x0F) << 4) | (compression & 0x0F)
    b3 = 0x00
    return bytes([b0, b1, b2, b3])


@dataclass
class VolcAsrClient:
    """Minimal client for Volcengine Streaming ASR (bigmodel_async)."""

    app_key: str
    access_key: str
    resource_id: str = DEFAULT_ASR_RESOURCE_ID

    async def transcribe_pcm16le_16k(self, pcm_bytes: bytes, language: str = "zh-CN") -> str:
        """
        Send one utterance (PCM S16LE, 16kHz, mono) to Volc ASR and return the final text.
        """
        if not pcm_bytes:
            return ""

        headers = _asr_auth_headers(self.app_key, self.access_key, self.resource_id)

        try:
            async with websockets.connect(
                ASR_WS_URL,
                additional_headers=headers,
                ping_interval=20,
                ping_timeout=10,
                close_timeout=5,
            ) as ws:
                # 1) Full client request: JSON + GZIP + seq，对齐官方
                full_req = {
                    "user": {"uid": "asr-demo"},
                    "audio": {
                        "format": "pcm",
                        "rate": 16000,
                        "bits": 16,
                        "channel": 1,
                        "language": language,
                    },
                    "request": {
                        "model_name": "bigmodel",
                        "enable_itn": True,
                        "enable_punc": True,
                    },
                }
                payload_bytes = json.dumps(full_req, ensure_ascii=False).encode("utf-8")
                compressed = gzip.compress(payload_bytes)
                header = _build_header(
                    message_type=0b0001,
                    flags=0b0001,  # POS_SEQUENCE
                    serialization=0b0001,
                    compression=0b0001,  # GZIP
                )
                frame = header + struct.pack(">i", 1) + struct.pack(">I", len(compressed)) + compressed
                await ws.send(frame)

                # 2) Audio-only: GZIP + seq，最后一包 seq 为负
                seq = 2
                offset = 0
                total = len(pcm_bytes)
                while offset < total:
                    chunk = pcm_bytes[offset : offset + CHUNK_BYTES]
                    offset += len(chunk)
                    is_last = offset >= total
                    comp_chunk = gzip.compress(chunk)
                    ah = _build_header(
                        message_type=0b0010,
                        flags=0b0011 if is_last else 0b0001,
                        serialization=0b0000,
                        compression=0b0001,
                    )
                    send_seq = -seq if is_last else seq
                    frame = ah + struct.pack(">i", send_seq) + struct.pack(">I", len(comp_chunk)) + comp_chunk
                    await ws.send(frame)
                    if not is_last:
                        seq += 1

                # 3) Read responses，对齐官方解析
                final_text = ""
                while True:
                    raw = await ws.recv()
                    if isinstance(raw, str):
                        continue
                    obj, is_last, is_error, err_code = _parse_asr_response(raw)
                    if is_error:
                        break
                    if obj:
                        text = _extract_text(obj)
                        if text:
                            final_text = text
                        if is_last:
                            break

                return final_text
        except Exception as exc:  # noqa: BLE001
            LOG.warning("Volc ASR transcribe failed: %s", exc)
            return ""


class VolcAsrStreamingSession:
    """流式 ASR 会话：麦克风流式输入 → ASR 流式输出。"""

    def __init__(
        self,
        app_key: str,
        access_key: str,
        *,
        resource_id: str = DEFAULT_ASR_RESOURCE_ID,
        language: str = "zh-CN",
        on_partial: Callable[[str, bool], Awaitable[None]] | None = None,
        on_server_error: Callable[[int | None], Awaitable[None]] | None = None,
        on_notify: Callable[[str, str], Awaitable[None]] | None = None,
    ):
        self.app_key = app_key
        self.access_key = access_key
        self.resource_id = resource_id
        self.language = language
        self.on_partial = on_partial
        self.on_server_error = on_server_error
        self.on_notify = on_notify
        self._ws = None
        self._recv_task: asyncio.Task | None = None
        self._final_text = ""
        self._last_error_code: int | None = None
        self._done = asyncio.Event()
        self._seq = 2

    async def start(self) -> None:
        self._last_error_code = None
        self._final_text = ""
        headers = _asr_auth_headers(self.app_key, self.access_key, self.resource_id)
        self._ws = await websockets.connect(
            ASR_WS_URL,
            additional_headers=headers,
            ping_interval=20,
            ping_timeout=10,
            close_timeout=5,
        )
        full_req = {
            "user": {"uid": "asr-demo"},
            "audio": {
                "format": "pcm",
                "rate": 16000,
                "bits": 16,
                "channel": 1,
                "language": self.language,
            },
            "request": {"model_name": "bigmodel", "enable_itn": True, "enable_punc": True},
        }
        payload_bytes = json.dumps(full_req, ensure_ascii=False).encode("utf-8")
        compressed = gzip.compress(payload_bytes)
        header = _build_header(
            message_type=0b0001,
            flags=0b0001,
            serialization=0b0001,
            compression=0b0001,
        )
        frame = header + struct.pack(">i", 1) + struct.pack(">I", len(compressed)) + compressed
        await self._ws.send(frame)
        self._recv_task = asyncio.create_task(self._recv_loop())

    async def send_audio(self, pcm_chunk: bytes, is_last: bool = False) -> None:
        if not self._ws:
            return
        comp = gzip.compress(pcm_chunk)
        ah = _build_header(
            message_type=0b0010,
            flags=0b0011 if is_last else 0b0001,
            serialization=0b0000,
            compression=0b0001,
        )
        send_seq = -self._seq if is_last else self._seq
        frame = ah + struct.pack(">i", send_seq) + struct.pack(">I", len(comp)) + comp
        await self._ws.send(frame)
        if not is_last:
            self._seq += 1

    def get_last_error_code(self) -> int | None:
        return self._last_error_code

    async def abort(self) -> None:
        """Close the ASR stream immediately (browser Stop / WS disconnect). No final transcript."""
        if self._recv_task and not self._recv_task.done():
            self._recv_task.cancel()
            try:
                await self._recv_task
            except asyncio.CancelledError:
                pass
        self._recv_task = None
        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None
        self._done.set()

    async def _recv_loop(self) -> None:
        try:
            while self._ws:
                raw = await self._ws.recv()
                if isinstance(raw, str):
                    continue
                obj, is_last, is_error, err_code = _parse_asr_response(raw)
                if is_error:
                    self._last_error_code = err_code
                    if self.on_server_error:
                        await self.on_server_error(err_code)
                    break
                if obj:
                    text = _extract_text(obj)
                    if not text and is_last and obj:
                        LOG.info("ASR last payload (no text): %s", json.dumps(obj, ensure_ascii=False)[:500])
                    if text:
                        self._final_text = text
                        if self.on_partial:
                            await self.on_partial(text, not is_last)
                    if is_last:
                        break
            self._done.set()
        except asyncio.CancelledError:
            self._done.set()
            raise
        except ConnectionClosed as e:
            if e.code != 1000:
                LOG.warning("ASR connection closed: %s", e)
                if self.on_notify:
                    try:
                        await self.on_notify("warning", f"ASR connection closed: {e}")
                    except Exception:
                        pass
            self._done.set()
        except Exception as exc:  # noqa: BLE001
            LOG.warning("ASR recv_loop error: %s", exc)
            if self.on_notify:
                try:
                    await self.on_notify("warning", f"ASR recv_loop error: {exc}")
                except Exception:
                    pass
            self._done.set()

    async def wait_final(self) -> str:
        await self._done.wait()
        if self._recv_task and not self._recv_task.done():
            self._recv_task.cancel()
            try:
                await self._recv_task
            except asyncio.CancelledError:
                pass
        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None
        return self._final_text

