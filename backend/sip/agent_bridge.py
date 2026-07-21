# -*- coding: utf-8 -*-
"""
Bridge SIP/RTP media ↔ Voice Agent (Duplex).

Scenario-agnostic: only needs system_role / speaking_style / call_id.
Audio path:
  RTP PCMU 8k → PCM16 16k → DuplexVoiceClient.send_audio
  Duplex TTS PCM16 24k → PCMU 8k → RtpSession.enqueue_pcmu
"""
from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from backend.duplex_client import DuplexVoiceClient
from backend.sip.codec import (
    AGENT_IN_RATE,
    AGENT_TTS_RATE,
    PcmuFrameBuffer,
    agent_tts_to_telephony,
    resample_pcm16,
    telephony_rx_to_agent,
)
from backend.sip.config import SipConfig
from backend.sip.recording import CallRecorder
from backend.sip.session import SipCallResult, SipCallSession

LOG = logging.getLogger(__name__)


@dataclass
class BridgeOutcome:
    sip: SipCallResult
    transcript: list[dict[str, str]] = field(default_factory=list)
    recordings: dict[str, str] = field(default_factory=dict)
    error: str = ""
    agent_session_id: str = ""


def _doubao_creds() -> tuple[str, str, str]:
    return (
        (os.environ.get("DOUBAO_API_KEY") or "").strip(),
        (os.environ.get("DOUBAO_APP_ID") or "").strip(),
        (os.environ.get("DOUBAO_ACCESS_KEY") or "").strip(),
    )


class SipAgentBridge:
    """Run one outbound SIP call with a live Voice Agent session."""

    def __init__(
        self,
        sip_config: SipConfig,
        *,
        system_role: str,
        speaking_style: str = "",
        call_id: str = "",
        bot_name: str = "语音助手",
        max_call_sec: float | None = None,
        opening_line: str | None = None,
        opening_nudge: str | None = None,
        on_status: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> None:
        self.sip_config = sip_config
        self.system_role = system_role
        self.speaking_style = speaking_style
        self.call_id = call_id or "anon"
        self.bot_name = bot_name
        self.max_call_sec = max_call_sec if max_call_sec is not None else sip_config.max_call_sec
        # 方案2: prefer explicit opening_line; fallback to config / legacy nudge.
        if opening_line is not None:
            line = opening_line
        elif opening_nudge is not None:
            line = opening_nudge
        else:
            line = sip_config.opening_nudge
        self.opening_line = (line or "").strip()
        self.on_status = on_status
        self._loop: asyncio.AbstractEventLoop | None = None
        self._uplink_q: asyncio.Queue[bytes | None] | None = None
        self._pcmu_tx_buf = PcmuFrameBuffer()
        self._transcript: list[dict[str, str]] = []
        self._assistant_buf = ""
        self._recorder: CallRecorder | None = None
        self._session: SipCallSession | None = None
        self._client: DuplexVoiceClient | None = None
        self._accept_uplink = False
        self._opening_tts_started: asyncio.Event | None = None
        self._opening_tts_done: asyncio.Event | None = None
        self._opening_uplink_pending = False

    def _emit(self, status: str, **extra: Any) -> None:
        if self.on_status:
            try:
                self.on_status(status, extra)
            except Exception:
                LOG.debug("on_status failed", exc_info=True)

    async def run(self, phone: str) -> BridgeOutcome:
        api_key, app_id, access_key = _doubao_creds()
        if not api_key and not (app_id and access_key):
            return BridgeOutcome(
                sip=SipCallResult(ok=False, error="Doubao credentials missing"),
                error="Doubao credentials missing (DOUBAO_API_KEY or APP_ID+ACCESS_KEY)",
            )

        if self.sip_config.record_dir:
            self._recorder = CallRecorder(self.call_id, self.sip_config.record_dir)

        self._loop = asyncio.get_running_loop()
        self._uplink_q = asyncio.Queue(maxsize=200)
        self._opening_tts_started = asyncio.Event()
        self._opening_tts_done = asyncio.Event()

        session = SipCallSession(self.sip_config)
        self._session = session

        def on_pcmu(payload: bytes) -> None:
            if not self._accept_uplink:
                return
            try:
                pcm16 = telephony_rx_to_agent(payload)
            except Exception:
                LOG.exception("telephony decode failed")
                return
            if not pcm16:
                return
            if self._recorder:
                self._recorder.append_user(pcm16)
            loop = self._loop
            q = self._uplink_q
            if loop and q:
                loop.call_soon_threadsafe(self._enqueue_uplink, pcm16)

        self._emit("dialing", phone=phone)
        sip_result = await asyncio.to_thread(session.dial, phone, on_pcmu=on_pcmu)
        if not sip_result.ok:
            self._emit("failed", error=sip_result.error)
            return BridgeOutcome(sip=sip_result, error=sip_result.error)

        self._emit(
            "answered",
            servercallid=sip_result.servercallid,
            remote_rtp=sip_result.remote_rtp,
        )

        session_ready = asyncio.Event()

        def on_session_started(_dialog_id: str) -> None:
            session_ready.set()

        client = DuplexVoiceClient(
            api_key=api_key,
            app_id=app_id,
            access_key=access_key,
            on_asr=self._on_asr,
            on_asr_ended=self._on_asr_ended,
            on_tts_audio=self._on_tts_audio,
            on_tts_start=self._on_tts_start,
            on_tts_end=self._on_tts_end,
            on_chat_response=self._on_chat_delta,
            on_chat_ended=self._on_chat_ended,
            on_session_started=on_session_started,
            on_error=lambda m: LOG.warning("agent error call_id=%s: %s", self.call_id, m),
        )
        self._client = client

        outcome_error = ""
        keepalive_task: asyncio.Task | None = None
        promote_task: asyncio.Task | None = None
        try:
            await client.connect()
            client.start_receive_loop()
            opening_first = bool(self.opening_line and self.sip_config.ai_speak_first)
            self._opening_uplink_pending = opening_first
            sid = await client.start_session(
                self.system_role,
                self.speaking_style,
                bot_name=self.bot_name,
                dialog_id=self.call_id,
                input_mod="audio",
                server_vad=True,
                recv_timeout=120,
            )
            self._emit("agent_started", session_id=sid)

            if opening_first:
                try:
                    await asyncio.wait_for(session_ready.wait(), timeout=5.0)
                except asyncio.TimeoutError:
                    LOG.warning(
                        "session.created timeout call_id=%s; sending SayHello anyway",
                        self.call_id,
                    )
                await client.say_hello(self.opening_line)
                LOG.info(
                    "SayHello opening call_id=%s chars=%d",
                    self.call_id,
                    len(self.opening_line),
                )
                self._emit("opening_line_sent", text_len=len(self.opening_line))
                self._transcript.append({"role": "assistant", "text": self.opening_line})
                promote_task = asyncio.create_task(
                    self._enable_uplink_after_opening(),
                    name=f"sip-opening-{self.call_id}",
                )
            else:
                self._accept_uplink = True

            uplink_task = asyncio.create_task(self._uplink_loop(client))
            keepalive_task = asyncio.create_task(self._silence_keepalive_watchdog(client, session))
            try:
                await asyncio.to_thread(session.wait_until_done, self.max_call_sec)
                if not session.is_done:
                    session.hangup(reason="max_duration")
                    outcome_error = "max call duration reached"
                else:
                    LOG.info(
                        "SIP call ended call_id=%s hangup_reason=%s rx=%s tx=%s",
                        self.call_id,
                        session.result.hangup_reason,
                        session.result.rx_packets,
                        session.result.tx_packets,
                    )
            finally:
                self._enqueue_uplink(None)
                for task in (promote_task, keepalive_task, uplink_task):
                    if task and not task.done():
                        task.cancel()
                for task in (promote_task, keepalive_task, uplink_task):
                    if not task:
                        continue
                    try:
                        await asyncio.wait_for(task, timeout=2.0)
                    except (asyncio.TimeoutError, asyncio.CancelledError):
                        pass
        except Exception as exc:
            LOG.exception("agent bridge failed call_id=%s", self.call_id)
            outcome_error = str(exc)
            session.hangup(reason="agent_error")
        finally:
            try:
                await client.disconnect()
            except Exception:
                LOG.debug("agent disconnect failed", exc_info=True)
            if not session.is_done:
                session.hangup(reason="bridge_done")

        recordings: dict[str, str] = {}
        if self._recorder:
            recordings = self._recorder.close()

        sip_final = session.result
        self._emit(
            "finished",
            hangup_reason=sip_final.hangup_reason,
            rx=sip_final.rx_packets,
            tx=sip_final.tx_packets,
        )
        return BridgeOutcome(
            sip=sip_final,
            transcript=list(self._transcript),
            recordings=recordings,
            error=outcome_error or sip_final.error,
            agent_session_id=self.call_id,
        )

    async def _enable_uplink_after_opening(self) -> None:
        """Gate RTP uplink until opening TTS finishes (avoid mid-greeting interference)."""
        assert self._opening_tts_done is not None
        try:
            try:
                await asyncio.wait_for(self._opening_tts_done.wait(), timeout=30.0)
                LOG.info("opening TTS done call_id=%s; enabling uplink", self.call_id)
            except asyncio.TimeoutError:
                LOG.warning(
                    "opening TTS not finished within 30s call_id=%s; enabling uplink anyway",
                    self.call_id,
                )
            await asyncio.sleep(0.15)
            self._accept_uplink = True
            self._opening_uplink_pending = False
            self._emit("audio_mode_ready")
        except asyncio.CancelledError:
            raise
        except Exception:
            LOG.exception("enable uplink after opening failed call_id=%s", self.call_id)
            self._accept_uplink = True

    def _enqueue_uplink(self, item: bytes | None) -> None:
        q = self._uplink_q
        if not q:
            return
        try:
            q.put_nowait(item)
        except asyncio.QueueFull:
            try:
                q.get_nowait()
            except asyncio.QueueEmpty:
                pass
            try:
                q.put_nowait(item)
            except asyncio.QueueFull:
                pass

    async def _uplink_loop(self, client: DuplexVoiceClient) -> None:
        assert self._uplink_q is not None
        while True:
            chunk = await self._uplink_q.get()
            if chunk is None:
                break
            try:
                await client.send_audio(chunk)
            except Exception:
                LOG.debug("uplink send_audio failed", exc_info=True)
                break

    async def _silence_keepalive_watchdog(
        self,
        client: DuplexVoiceClient,
        session: SipCallSession,
    ) -> None:
        """
        Duplex stops silence keepalive after first real mic/RTP audio.
        On phone calls, gaps between turns exceed recv_timeout and the dialog dies.
        Restart keepalive whenever it is not running.
        """
        try:
            while not session.is_done:
                task = getattr(client, "_silence_keepalive_task", None)
                if task is None or task.done():
                    try:
                        await client.start_silence_keepalive()
                    except Exception:
                        LOG.debug("restart duplex silence keepalive failed", exc_info=True)
                await asyncio.sleep(3.0)
        except asyncio.CancelledError:
            raise

    def _on_asr(self, text: str, is_interim: bool) -> None:
        if is_interim or not text:
            return
        # Finals also come via on_asr_ended; avoid double if both fire with finals.
        pass

    def _on_asr_ended(self, text: str) -> None:
        text = (text or "").strip()
        if text:
            self._transcript.append({"role": "user", "text": text})

    def _on_chat_delta(self, delta: str, _item_id: str, _reply_id: str) -> None:
        if delta:
            self._assistant_buf += delta

    def _on_chat_ended(self, _item_id: str, _reply_id: str) -> None:
        text = self._assistant_buf.strip()
        self._assistant_buf = ""
        if text:
            self._transcript.append({"role": "assistant", "text": text})

    def _on_tts_start(self, _meta: dict) -> None:
        if self._opening_tts_started and not self._opening_tts_started.is_set():
            self._opening_tts_started.set()

    def _on_tts_end(self, _meta: dict) -> None:
        if self._opening_uplink_pending and self._opening_tts_done and not self._opening_tts_done.is_set():
            self._opening_tts_done.set()
            LOG.info("opening TTS ended call_id=%s", self.call_id)
        client = self._client
        if not client:
            return
        # After AI finishes speaking, ensure duplex keepalive is running again.
        try:
            loop = self._loop
            if loop and loop.is_running():
                loop.create_task(client.start_silence_keepalive())
        except Exception:
            LOG.debug("tts_end restart keepalive failed", exc_info=True)

    def _on_tts_audio(self, pcm24k: bytes) -> None:
        if not pcm24k or not self._session:
            return
        if self._opening_tts_started and not self._opening_tts_started.is_set():
            self._opening_tts_started.set()
        # Record AI at 16k for consistent mixed track rate.
        if self._recorder:
            pcm16 = resample_pcm16(pcm24k, AGENT_TTS_RATE, AGENT_IN_RATE)
            self._recorder.append_ai(pcm16)
        try:
            pcmu = agent_tts_to_telephony(pcm24k, tts_rate=AGENT_TTS_RATE)
        except Exception:
            LOG.exception("TTS→PCMU failed")
            return
        for frame in self._pcmu_tx_buf.push(pcmu):
            self._session.enqueue_pcmu(frame)
