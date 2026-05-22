from __future__ import annotations

import logging
import uuid
from typing import Any

from backend.duplug_vad_client import DuplugTurnState, DuplugVadSession, duplug_server_url
from backend.duplug_vad_local import DuplugLocalVadManager
from backend.vad.types import VadEvent

LOG = logging.getLogger(__name__)

_LOCAL_MANAGER: DuplugLocalVadManager | None = None


def init_duplug_local_manager(config_path: str) -> DuplugLocalVadManager | None:
    global _LOCAL_MANAGER
    try:
        mgr = DuplugLocalVadManager(config_path=config_path)
        mgr.load_once()
        _LOCAL_MANAGER = mgr
        return mgr
    except Exception as exc:  # noqa: BLE001
        LOG.warning("Duplug local VAD init failed: %s", exc)
        _LOCAL_MANAGER = None
        return None


def get_duplug_local_manager() -> DuplugLocalVadManager | None:
    return _LOCAL_MANAGER


def shutdown_duplug_local_manager() -> None:
    global _LOCAL_MANAGER
    _LOCAL_MANAGER = None


def _map_duplug_state(st: DuplugTurnState) -> list[VadEvent]:
    events: list[VadEvent] = []
    if st.state == "nonidle":
        events.append(
            VadEvent(kind="speech_start", provider="duplug", state="nonidle")
        )
    elif st.state == "speak":
        events.append(
            VadEvent(kind="turn_end_candidate", provider="duplug", state="speak")
        )
    elif st.state == "idle":
        events.append(VadEvent(kind="silence", provider="duplug", state="idle"))
    return events


class DuplugBackend:
    provider = "duplug"

    def __init__(
        self,
        *,
        local_manager: DuplugLocalVadManager | None,
        remote_url: str,
    ) -> None:
        self._local = local_manager
        self._remote_url = remote_url
        self._session_id = ""
        self._remote: DuplugVadSession | None = None

    async def start_session(self, session_id: str) -> dict[str, Any]:
        self._session_id = session_id or uuid.uuid4().hex
        if self._local is not None:
            self._local.ensure_session(self._session_id)
            return {
                "provider": self.provider,
                "enabled": True,
                "mode": "local",
            }
        self._remote = DuplugVadSession(server_url=self._remote_url, session_id=self._session_id)
        await self._remote.connect()
        return {
            "provider": self.provider,
            "enabled": True,
            "mode": "remote",
            "server_url": self._remote_url,
        }

    async def feed_pcm_s16le_16k(self, pcm_chunk: bytes) -> list[VadEvent]:
        st: DuplugTurnState | None = None
        if self._local is not None and self._session_id:
            st = self._local.feed_pcm_s16le_16k(self._session_id, pcm_chunk)
        elif self._remote is not None:
            st = await self._remote.feed_pcm_s16le_16k(pcm_chunk)
        if st is None:
            return []
        return _map_duplug_state(st)

    async def close_session(self) -> None:
        if self._local is not None and self._session_id:
            self._local.close_session(self._session_id)
        if self._remote is not None:
            await self._remote.close()
        self._session_id = ""
        self._remote = None
