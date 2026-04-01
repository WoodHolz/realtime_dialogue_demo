from __future__ import annotations

import base64
import importlib
import logging
import os
import time
from dataclasses import dataclass
from typing import Any

import numpy as np

from backend.duplug_vad_client import DuplugTurnState

LOG = logging.getLogger(__name__)


def _load_symbol(module_name: str, symbol: str) -> Any:
    mod = importlib.import_module(module_name)
    return getattr(mod, symbol)


@dataclass
class _LocalSession:
    session: Any
    last_touch_ts: float

    def touch(self) -> None:
        self.last_touch_ts = time.time()


class DuplugLocalVadManager:
    """
    In-process Duplug VAD integration.

    Expected upstream symbols:
    - service.model.load_turn_model
    - service.engine.TurnTakingEngine
    - service.session.TurnSession
    """

    def __init__(self, *, config_path: str, session_ttl_s: float = 120.0) -> None:
        self.config_path = config_path
        self.session_ttl_s = session_ttl_s
        self.model: Any | None = None
        self._sessions: dict[str, _LocalSession] = {}

        self._load_turn_model = _load_symbol("service.model", "load_turn_model")
        self._engine_cls = _load_symbol("service.engine", "TurnTakingEngine")
        self._session_cls = _load_symbol("service.session", "TurnSession")

    def load_once(self) -> None:
        if self.model is not None:
            return
        LOG.info("[DuplugLocal] loading model from config=%s", self.config_path)
        self.model = self._load_turn_model(config_path=self.config_path)
        LOG.info("[DuplugLocal] model loaded")

    def _gc_expired(self) -> None:
        if not self._sessions:
            return
        now = time.time()
        expired = [
            sid
            for sid, sess in self._sessions.items()
            if now - sess.last_touch_ts > self.session_ttl_s
        ]
        for sid in expired:
            self._sessions.pop(sid, None)
        if expired:
            LOG.info("[DuplugLocal] gc removed %d sessions", len(expired))

    def ensure_session(self, session_id: str) -> None:
        self.load_once()
        self._gc_expired()
        if session_id in self._sessions:
            self._sessions[session_id].touch()
            return

        if self.model is None:
            raise RuntimeError("Duplug local model is not loaded")
        engine = self._engine_cls(model=self.model)
        turn_session = self._session_cls(engine)
        self._sessions[session_id] = _LocalSession(session=turn_session, last_touch_ts=time.time())
        LOG.info("[DuplugLocal] new session %s", session_id)

    def close_session(self, session_id: str) -> None:
        self._sessions.pop(session_id, None)

    def feed_pcm_s16le_16k(self, session_id: str, pcm_chunk: bytes) -> DuplugTurnState | None:
        if not pcm_chunk:
            return None
        self.ensure_session(session_id)
        wrapped = self._sessions.get(session_id)
        if wrapped is None:
            return None
        wrapped.touch()

        # Convert little-endian int16 PCM to float32 in [-1, 1].
        audio_i16 = np.frombuffer(pcm_chunk, dtype="<i2")
        if audio_i16.size == 0:
            return None
        audio = audio_i16.astype(np.float32)
        audio /= np.where(audio < 0, 32768.0, 32767.0)

        state = wrapped.session.feed_audio(audio)
        if not isinstance(state, dict):
            return None
        return DuplugTurnState.from_payload(state)


def duplug_local_enabled() -> bool:
    v = (os.environ.get("DUPLUG_VAD_LOCAL_ENABLED", "") or "").strip().lower()
    return v in {"1", "true", "yes", "y", "on"}


def duplug_local_config_path() -> str:
    # Default to Duplug's conventional config path when repo is available in PYTHONPATH.
    default = "config/config.yaml"
    return (os.environ.get("DUPLUG_VAD_LOCAL_CONFIG", "") or "").strip() or default


def decode_duplug_float32_b64(audio_b64: str) -> np.ndarray:
    """
    Utility kept for protocol parity checks and testing.
    Decode base64(float32_bytes) -> float32 ndarray.
    """
    raw = base64.b64decode(audio_b64)
    return np.frombuffer(raw, dtype=np.float32)
