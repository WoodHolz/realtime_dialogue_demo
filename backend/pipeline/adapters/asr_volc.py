# -*- coding: utf-8 -*-
"""Volc streaming ASR adapter wrapping VolcAsrStreamingSession."""
from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable

from backend.asr_client import CHUNK_BYTES, DEFAULT_ASR_RESOURCE_ID, VolcAsrStreamingSession

LOG = logging.getLogger(__name__)

AsrPartialCb = Callable[[str, bool], Awaitable[None]]
AsrErrorCb = Callable[[int | None], Awaitable[None]]


class VolcAsrAdapter:
    def __init__(
        self,
        app_key: str,
        access_key: str,
        *,
        resource_id: str = DEFAULT_ASR_RESOURCE_ID,
        language: str = "zh-CN",
        on_partial: AsrPartialCb | None = None,
        on_error: AsrErrorCb | None = None,
        keepalive_idle_sec: float = 2.0,
        keepalive_tick_sec: float = 0.2,
    ) -> None:
        self.app_key = app_key
        self.access_key = access_key
        self.resource_id = resource_id
        self.language = language
        self.on_partial = on_partial
        self.on_error = on_error
        self._keepalive_idle_sec = keepalive_idle_sec
        self._keepalive_tick_sec = keepalive_tick_sec
        self._session: VolcAsrStreamingSession | None = None
        self._keepalive_task: asyncio.Task | None = None
        self._last_real_send = 0.0
        self._closed = False
        self._silence = bytes(CHUNK_BYTES)

    def bind(self, *, on_partial: AsrPartialCb, on_error: AsrErrorCb | None = None) -> None:
        self.on_partial = on_partial
        self.on_error = on_error

    async def start(self, *, turn_id: str = "") -> None:
        self._closed = False

        async def _partial(text: str, is_interim: bool) -> None:
            if self.on_partial:
                await self.on_partial(text, not is_interim)

        async def _error(code: int | None) -> None:
            if self.on_error:
                await self.on_error(code)

        self._session = VolcAsrStreamingSession(
            self.app_key,
            self.access_key,
            resource_id=self.resource_id,
            language=self.language,
            on_partial=_partial,
            on_server_error=_error,
        )
        await self._session.start()
        self._last_real_send = time.monotonic()
        self._keepalive_task = asyncio.create_task(self._keepalive_loop())
        LOG.debug("volc ASR adapter started turn_id=%s", turn_id)

    async def send_pcm(self, pcm16_16k: bytes, *, is_last: bool = False) -> None:
        if not self._session:
            return
        if pcm16_16k and not is_last:
            self._last_real_send = time.monotonic()
        await self._session.send_audio(pcm16_16k, is_last=is_last)
        if is_last:
            await self._stop_keepalive()

    async def wait_final(self) -> str:
        if not self._session:
            return ""
        text = await self._session.wait_final()
        await self._stop_keepalive()
        self._session = None
        return text or ""

    async def cancel(self) -> None:
        self._closed = True
        await self._stop_keepalive()
        sess = self._session
        self._session = None
        if sess:
            try:
                await sess.abort()
            except Exception:
                LOG.debug("volc ASR abort failed", exc_info=True)

    async def _stop_keepalive(self) -> None:
        task = self._keepalive_task
        self._keepalive_task = None
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def _keepalive_loop(self) -> None:
        try:
            while not self._closed and self._session:
                await asyncio.sleep(self._keepalive_tick_sec)
                if self._closed or not self._session:
                    return
                idle = time.monotonic() - self._last_real_send
                if idle >= self._keepalive_idle_sec:
                    await self._session.send_audio(self._silence, is_last=False)
        except asyncio.CancelledError:
            return
        except Exception:
            LOG.debug("volc ASR keepalive stopped", exc_info=True)
