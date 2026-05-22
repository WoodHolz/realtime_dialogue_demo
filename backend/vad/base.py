from __future__ import annotations

from typing import Protocol

from backend.vad.types import VadEvent


class VadBackend(Protocol):
    provider: str

    async def start_session(self, session_id: str) -> dict:
        """Prepare a turn session. Returns vad_status payload fields (without type)."""
        ...

    async def feed_pcm_s16le_16k(self, pcm_chunk: bytes) -> list[VadEvent]:
        ...

    async def close_session(self) -> None:
        ...
