# -*- coding: utf-8 -*-
"""
Doubao Realtime Voice API binary WebSocket protocol.

Protocol: 4-byte header + optional (event, session_id, etc.) + 4-byte payload_size + payload.
Ref: API doc section 2.2 WebSocket二进制协议
"""
from __future__ import annotations

import json
import struct
import uuid
from typing import Any

# Protocol version and header size
PROTOCOL_VERSION = 0x01
HEADER_SIZE = 0x01

# Message types (left 4 bits of byte 1)
MSG_CLIENT_TEXT = 0x01
MSG_SERVER_TEXT = 0x09
MSG_AUDIO_REQUEST = 0x02
MSG_AUDIO_RESPONSE = 0x0B
MSG_ERROR = 0x0F

# Serialization
SER_RAW = 0x00
SER_JSON = 0x01

# Compression
COMPRESS_NONE = 0x00

# Message type specific flags (right 4 bits): 0b0100 = has event
FLAG_HAS_EVENT = 0x04
# Sequence: 0b0000 none, 0b0001 non-terminal, 0b0010 last no seq, 0b0011 last seq -1
FLAG_SEQ_NONE = 0x00
FLAG_SEQ_LAST = 0x02

# Event IDs (client)
EVENT_START_CONNECTION = 1
EVENT_FINISH_CONNECTION = 2
EVENT_START_SESSION = 100
EVENT_FINISH_SESSION = 102
EVENT_TASK_REQUEST = 200
EVENT_SAY_HELLO = 300
EVENT_CHAT_TTS_TEXT = 500
EVENT_CHAT_TEXT_QUERY = 501  # ChatTextQuery
EVENT_CONVERSATION_CREATE = 510
EVENT_CONVERSATION_UPDATE = 511

# Event IDs (server)
EVENT_CONNECTION_STARTED = 50
EVENT_SESSION_STARTED = 150
EVENT_SESSION_FINISHED = 152
EVENT_SESSION_FAILED = 153
EVENT_ASR_INFO = 450
EVENT_ASR_RESPONSE = 451
EVENT_ASR_ENDED = 459
EVENT_TTS_SENTENCE_START = 350
EVENT_TTS_SENTENCE_END = 351
EVENT_TTS_RESPONSE = 352
EVENT_TTS_ENDED = 359
EVENT_CHAT_RESPONSE = 550
EVENT_CHAT_ENDED = 559
EVENT_DIALOG_COMMON_ERROR = 599


def _pack_header(
    message_type: int,
    serialization: int = SER_JSON,
    flags: int = FLAG_HAS_EVENT,
) -> bytes:
    byte0 = (PROTOCOL_VERSION << 4) | HEADER_SIZE
    byte1 = (message_type << 4) | flags
    byte2 = (serialization << 4) | COMPRESS_NONE
    byte3 = 0x00
    return struct.pack(">BBBB", byte0, byte1, byte2, byte3)


def _pack_optional_event_session(session_id: str | None) -> bytes:
    buf = b""
    if session_id:
        sid = session_id.encode("utf-8")
        buf += struct.pack(">I", len(sid))
        buf += sid
    return buf


def encode_client_json_event(
    event_id: int,
    payload_obj: dict[str, Any] | None,
    session_id: str | None = None,
) -> bytes:
    """Encode a client text (JSON) event frame."""
    payload = json.dumps(payload_obj or {}, ensure_ascii=False).encode("utf-8")
    header = _pack_header(MSG_CLIENT_TEXT, SER_JSON, FLAG_HAS_EVENT)
    optional = struct.pack(">I", event_id)
    optional += _pack_optional_event_session(session_id)
    optional += struct.pack(">I", len(payload))
    return header + optional + payload


def encode_audio_request(payload_bytes: bytes, session_id: str | None) -> bytes:
    """Encode TaskRequest (audio) event. Payload is raw audio binary."""
    header = _pack_header(MSG_AUDIO_REQUEST, SER_RAW, FLAG_HAS_EVENT)
    optional = struct.pack(">I", EVENT_TASK_REQUEST)
    optional += _pack_optional_event_session(session_id)
    optional += struct.pack(">I", len(payload_bytes))
    return header + optional + payload_bytes


def decode_server_frame(data: bytes) -> list[tuple[int, dict | bytes]]:
    """
    Decode server binary frame(s). Returns list of (event_id, payload).
    payload is dict for JSON events, bytes for audio/raw.
    """
    results = []
    offset = 0
    while offset < len(data):
        if offset + 4 > len(data):
            break
        byte0, byte1, byte2, byte3 = struct.unpack(">BBBB", data[offset : offset + 4])
        offset += 4
        msg_type = (byte1 >> 4) & 0x0F
        flags = byte1 & 0x0F
        serialization = (byte2 >> 4) & 0x0F

        # optional: event
        event_id = 0
        if flags & FLAG_HAS_EVENT and offset + 4 <= len(data):
            event_id, = struct.unpack(">I", data[offset : offset + 4])
            offset += 4

        # optional: session id (session-level events carry this)
        if event_id not in (50, 51, 52):  # connection-level events have no session_id
            if offset + 4 <= len(data):
                sid_len, = struct.unpack(">I", data[offset : offset + 4])
                offset += 4
                if sid_len > 0 and offset + sid_len <= len(data):
                    offset += sid_len

        # payload size
        if offset + 4 > len(data):
            break
        payload_size, = struct.unpack(">I", data[offset : offset + 4])
        offset += 4
        if offset + payload_size > len(data):
            break
        payload = data[offset : offset + payload_size]
        offset += payload_size

        if serialization == SER_JSON and payload:
            try:
                payload = json.loads(payload.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                pass
        results.append((event_id, payload))
    return results


def new_session_id() -> str:
    return str(uuid.uuid4())
