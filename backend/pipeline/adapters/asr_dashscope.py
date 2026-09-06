# -*- coding: utf-8 -*-
"""Pipeline StreamingAsr adapter over DashScope Paraformer realtime."""
from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from backend.dashscope_asr_client import CHUNK_BYTES, DashScopeAsrStreamingSession

LOG = logging.getLogger(__name__)

AsrPartialCb = Callable[[str, bool], Awaitable[None]]
AsrErrorCb = Callable[[int | None], Awaitable[None]]


class DashScopeAsrAdapter:
    def __init__(
        self,
        *,
        on_partial: AsrPartialCb | None = None,
        on_error: AsrErrorCb | None = None,
    ) -> None:
        self.on_partial = on_partial
        self.on_error = on_error
        self._session: DashScopeAsrStreamingSession | None = None
        self._buf = bytearray()

    def bind(self, *, on_partial: AsrPartialCb, on_error: AsrErrorCb | None = None) -> None:
        self.on_partial = on_partial
        self.on_error = on_error

    async def start(self, *, turn_id: str = "") -> None:
        async def _partial(text: str, is_interim: bool) -> None:
            if self.on_partial:
                # Pipeline expects (text, is_final)
                await self.on_partial(text, not is_interim)

        async def _error(code: int | None) -> None:
            if self.on_error:
                await self.on_error(code)

        self._buf.clear()
        self._session = DashScopeAsrStreamingSession(
            on_partial=_partial,
            on_server_error=_error,
        )
        await self._session.start()
        LOG.debug("DashScope ASR adapter started turn_id=%s", turn_id)

    async def send_pcm(self, pcm16_16k: bytes, *, is_last: bool = False) -> None:
        if not self._session:
            return
        if pcm16_16k:
            self._buf.extend(pcm16_16k)
            while len(self._buf) >= CHUNK_BYTES:
                chunk = bytes(self._buf[:CHUNK_BYTES])
                del self._buf[:CHUNK_BYTES]
                await self._session.send_audio(chunk, is_last=False)
        if is_last:
            rem = bytes(self._buf)
            self._buf.clear()
            await self._session.send_audio(rem, is_last=True)

    async def wait_final(self) -> str:
        if not self._session:
            return ""
        text = await self._session.wait_final()
        self._session = None
        return text or ""

    async def cancel(self) -> None:
        sess = self._session
        self._session = None
        self._buf.clear()
        if sess:
            try:
                await sess.abort()
            except Exception:
                LOG.debug("DashScope ASR abort failed", exc_info=True)
