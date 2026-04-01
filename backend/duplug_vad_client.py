from __future__ import annotations

import asyncio
import base64
import json
import logging
import math
import os
import struct
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import websockets

try:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
except ImportError:
    pass
from websockets.exceptions import ConnectionClosed

LOG = logging.getLogger(__name__)


def _parse_bool(value: object, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}
    if isinstance(value, (int, float)):
        return bool(value)
    return default


def _load_duplug_vad_config() -> tuple[bool, str]:
    """
    Load Duplug VAD config from config.json with env override (.env via load_dotenv).

    Priority:
    1) Env: DUPLUG_VAD_ENABLED / DUPLUG_VAD_URL
    2) config.json: vad.duplug_enabled / vad.duplug_url
    3) defaults (disabled, ws://127.0.0.1:7997/turn)
    """
    enabled = False
    server_url = "ws://127.0.0.1:7997/turn"
    try:
        root = Path(__file__).resolve().parent.parent
        cfg_path = root / "config.json"
        if cfg_path.exists():
            with cfg_path.open("r", encoding="utf-8") as f:
                data = json.load(f)
            vad_cfg = data.get("vad", {}) or {}
            enabled = _parse_bool(vad_cfg.get("duplug_enabled", False), default=False)
            server_url = str(vad_cfg.get("duplug_url", server_url) or server_url)
    except Exception:
        pass

    env_enabled = os.environ.get("DUPLUG_VAD_ENABLED")
    if env_enabled is not None:
        enabled = _parse_bool(env_enabled, default=enabled)
    env_url = (os.environ.get("DUPLUG_VAD_URL", "") or "").strip()
    if env_url:
        server_url = env_url
    return enabled, server_url


_DUPLUG_ENABLED, _DUPLUG_SERVER_URL = _load_duplug_vad_config()


def _debug_rms_enabled() -> bool:
    v = (os.environ.get("DUPLUG_DEBUG_RMS", "") or "").strip().lower()
    return v in {"1", "true", "yes", "y", "on"}


def _rms_from_s16le_bytes(pcm_s16le: bytes) -> float:
    n = len(pcm_s16le) // 2
    if n <= 0:
        return 0.0
    mv = memoryview(pcm_s16le)
    s2 = 0.0
    for i in range(n):
        s = struct.unpack_from("<h", mv, i * 2)[0] / 32768.0
        s2 += s * s
    return math.sqrt(s2 / n)


def _pcm_s16le_to_f32le_bytes(pcm_s16le: bytes) -> bytes:
    """Convert 16kHz mono PCM s16le bytes into float32 bytes (official style)."""
    if not pcm_s16le:
        return b""
    n = len(pcm_s16le) // 2
    if n <= 0:
        return b""
    out = bytearray(n * 4)
    mv = memoryview(pcm_s16le)
    for i in range(n):
        s = struct.unpack_from("<h", mv, i * 2)[0]
        # Official example sends float32 chunks directly.
        # For browser-captured PCM s16le, convert to normalized float32 once here.
        f = s / 32768.0 if s < 0 else s / 32767.0
        struct.pack_into("<f", out, i * 4, float(f))
    return bytes(out)


@dataclass
class DuplugTurnState:
    state: str
    text: str | None = None
    asr_segment: str | None = None
    asr_buffer: str | None = None

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "DuplugTurnState | None":
        st = payload.get("state")
        if not isinstance(st, str) or not st:
            return None
        return cls(
            state=st,
            text=payload.get("text"),
            asr_segment=payload.get("asr_segment"),
            asr_buffer=payload.get("asr_buffer"),
        )


class DuplugVadSession:
    """
    Async client for SoulX-Duplug server.

    Protocol (per upstream server.py / example_client.py):
    - WebSocket endpoint: ws://host:7997/turn
    - Send text frame JSON:
      {"type":"audio","session_id":"...","audio":"base64(float32_bytes)"}
    - Receive text frame JSON:
      {"type":"turn_state","session_id":"...","state":{...},"ts":...}
    """

    def __init__(
        self,
        *,
        server_url: str,
        session_id: str | None = None,
        recv_timeout_s: float = 0.5,
        chunk_samples: int = 2560,
    ) -> None:
        self.server_url = server_url
        self.session_id = session_id or uuid.uuid4().hex
        self.recv_timeout_s = recv_timeout_s
        self.chunk_samples = max(1, int(chunk_samples))
        self._chunk_bytes_s16le = self.chunk_samples * 2
        self._ws: websockets.WebSocketClientProtocol | None = None
        self._lock = asyncio.Lock()
        self._last_state: DuplugTurnState | None = None
        self._pcm_s16le_buffer = bytearray()
        self._last_rms_log_ts = 0.0

    async def connect(self) -> None:
        if self._ws is not None:
            return
        self._ws = await websockets.connect(self.server_url, ping_interval=20, ping_timeout=10)

    async def close(self) -> None:
        ws = self._ws
        self._ws = None
        self._pcm_s16le_buffer.clear()
        if ws is not None:
            try:
                await ws.close()
            except Exception:
                pass

    async def feed_pcm_s16le_16k(self, pcm_chunk: bytes) -> DuplugTurnState | None:
        """
        Feed a PCM chunk (s16le 16k mono) and return the newest turn state if produced.

        Note: Duplug may not emit a state on every chunk (e.g. 'blank' internal caching).
        """
        if not pcm_chunk:
            return None

        async with self._lock:
            try:
                if self._ws is None:
                    await self.connect()
                if self._ws is None:
                    return None

                self._pcm_s16le_buffer.extend(pcm_chunk)
                latest: DuplugTurnState | None = None
                while len(self._pcm_s16le_buffer) >= self._chunk_bytes_s16le:
                    send_s16 = bytes(self._pcm_s16le_buffer[: self._chunk_bytes_s16le])
                    del self._pcm_s16le_buffer[: self._chunk_bytes_s16le]
                    if _debug_rms_enabled():
                        now = time.time()
                        if now - self._last_rms_log_ts >= 1.0:
                            self._last_rms_log_ts = now
                            LOG.info(
                                "Duplug input rms=%.5f chunk_samples=%d",
                                _rms_from_s16le_bytes(send_s16),
                                self.chunk_samples,
                            )

                    audio_f32 = _pcm_s16le_to_f32le_bytes(send_s16)
                    payload = {
                        "type": "audio",
                        "session_id": self.session_id,
                        "audio": base64.b64encode(audio_f32).decode("ascii"),
                    }
                    await self._ws.send(json.dumps(payload, ensure_ascii=False))

                    try:
                        raw = await asyncio.wait_for(self._ws.recv(), timeout=self.recv_timeout_s)
                    except asyncio.TimeoutError:
                        continue

                    if not isinstance(raw, str):
                        continue
                    data = json.loads(raw)
                    if not isinstance(data, dict) or data.get("type") != "turn_state":
                        continue
                    st = data.get("state")
                    if not isinstance(st, dict):
                        continue
                    parsed = DuplugTurnState.from_payload(st)
                    if parsed is not None:
                        self._last_state = parsed
                        latest = parsed
                return latest
            except (ConnectionClosed, OSError) as exc:
                LOG.warning("Duplug VAD connection dropped: %s", exc)
                await self.close()
                return None
            except Exception as exc:  # noqa: BLE001
                LOG.warning("Duplug VAD feed failed: %s", exc)
                await self.close()
                return None


def duplug_enabled() -> bool:
    return _DUPLUG_ENABLED


def duplug_server_url() -> str:
    return _DUPLUG_SERVER_URL

