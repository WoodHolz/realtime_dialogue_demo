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
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from backend.duplex_client import OUTBOUND_SPEAKER, DuplexVoiceClient
from backend.latency_logger import LatencyLogger
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
    transcript: list[dict[str, Any]] = field(default_factory=list)
    recordings: dict[str, str] = field(default_factory=dict)
    turn_timings: list[dict[str, Any]] = field(default_factory=list)
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
        voice: str = OUTBOUND_SPEAKER,
        max_call_sec: float | None = None,
        opening_line: str | None = None,
        opening_parts: list[str] | None = None,
        opening_nudge: str | None = None,
        on_status: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> None:
        self.sip_config = sip_config
        self.system_role = system_role
        self.speaking_style = speaking_style
        self.call_id = call_id or "anon"
        self.bot_name = bot_name
        self.voice = (voice or OUTBOUND_SPEAKER).strip() or OUTBOUND_SPEAKER
        self.max_call_sec = max_call_sec if max_call_sec is not None else sip_config.max_call_sec
        # 方案2: prefer explicit opening_line; fallback to config / legacy nudge.
        if opening_line is not None:
            line = opening_line
        elif opening_nudge is not None:
            line = opening_nudge
        else:
            line = sip_config.opening_nudge
        self.opening_line = (line or "").strip()
        if opening_parts:
            self.opening_parts = [p.strip() for p in opening_parts if (p or "").strip()]
        elif self.opening_line:
            self.opening_parts = [self.opening_line]
        else:
            self.opening_parts = []
        self.on_status = on_status
        self._loop: asyncio.AbstractEventLoop | None = None
        self._uplink_q: asyncio.Queue[bytes | None] | None = None
        self._pcmu_tx_buf = PcmuFrameBuffer()
        self._transcript: list[dict[str, Any]] = []
        self._assistant_buf = ""
        self._recorder: CallRecorder | None = None
        self._session: SipCallSession | None = None
        self._client: DuplexVoiceClient | None = None
        self._accept_uplink = False
        self._opening_tts_started: asyncio.Event | None = None
        self._opening_tts_done: asyncio.Event | None = None
        self._opening_uplink_pending = False
        self._opening_part_pcm24_bytes = 0
        self._opening_received_audio = False
        self._opening_settle_token = 0
        # SIP turn latency: user speech end → AI speech start (opening is AI-first, skipped).
        self._t0: float | None = None
        self._latency = LatencyLogger()
        self._turn_timings: list[dict[str, Any]] = []
        self._opening_active = False
        self._opening_tts_start_ms: int | None = None
        self._pending_user_end_ms: int | None = None
        self._pending_turn_latency_ms: float | None = None
        self._asr_interim = ""

    def _emit(self, status: str, **extra: Any) -> None:
        if self.on_status:
            try:
                self.on_status(status, extra)
            except Exception:
                LOG.debug("on_status failed", exc_info=True)

    def _rel_ms(self) -> int:
        if self._t0 is None:
            return 0
        return max(0, int((time.perf_counter() - self._t0) * 1000))

    def _publish_transcript(self) -> None:
        self._emit(
            "transcript_update",
            transcript=list(self._transcript),
            turn_timings=list(self._turn_timings),
            asr_interim=self._asr_interim,
        )

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
            try:
                pcm16 = telephony_rx_to_agent(payload)
            except Exception:
                LOG.exception("telephony decode failed")
                return
            if not pcm16:
                return
            # Always record user leg for stereo timeline (even while uplink gated).
            if self._recorder:
                self._recorder.append_user(pcm16)
            if not self._accept_uplink:
                return
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
        self._t0 = time.perf_counter()
        if self._recorder:
            self._recorder.mark_start()

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
            on_barge_in=self._on_barge_in,
            on_session_started=on_session_started,
            on_error=lambda m: LOG.warning("agent error call_id=%s: %s", self.call_id, m),
        )
        self._client = client

        outcome_error = ""
        keepalive_task: asyncio.Task | None = None
        try:
            await client.connect()
            client.start_receive_loop()
            opening_first = bool(self.opening_parts and self.sip_config.ai_speak_first)
            self._opening_uplink_pending = opening_first
            self._opening_active = opening_first
            sid = await client.start_session(
                self.system_role,
                self.speaking_style,
                bot_name=self.bot_name,
                dialog_id=self.call_id,
                input_mod="audio",
                server_vad=True,
                recv_timeout=120,
                voice=self.voice,
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
                await self._play_opening_parts(client)
                LOG.info(
                    "SayHello opening call_id=%s parts=%d chars=%d",
                    self.call_id,
                    len(self.opening_parts),
                    len(self.opening_line),
                )
                self._emit(
                    "opening_line_sent",
                    text_len=len(self.opening_line),
                    parts=len(self.opening_parts),
                )
                self._transcript.append(
                    {
                        "role": "assistant",
                        "text": self.opening_line,
                        "kind": "opening",
                        "source": "say_hello",
                        "t_ms": self._opening_tts_start_ms if self._opening_tts_start_ms is not None else self._rel_ms(),
                    }
                )
                self._turn_timings.append(
                    {
                        "kind": "opening",
                        "ai_speech_start_ms": self._opening_tts_start_ms,
                        "skipped_user_latency": True,
                        "note": "AI speak-first opening; no user→AI turn latency",
                    }
                )
                self._opening_active = False
                self._publish_transcript()
            else:
                self._accept_uplink = True
                self._opening_active = False

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
                for task in (keepalive_task, uplink_task):
                    if task and not task.done():
                        task.cancel()
                for task in (keepalive_task, uplink_task):
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
            turn_timings=list(self._turn_timings),
            error=outcome_error or sip_final.error,
            agent_session_id=self.call_id,
        )

    async def _play_opening_parts(self, client: DuplexVoiceClient) -> None:
        """Play opening as separate SayHello utterances; enable uplink when all finish."""
        assert self._opening_tts_done is not None
        for idx, part in enumerate(self.opening_parts):
            self._opening_tts_done.clear()
            self._opening_part_pcm24_bytes = 0
            self._opening_received_audio = False
            self._opening_settle_token += 1
            await client.say_hello(part)
            LOG.info(
                "SayHello part %d/%d call_id=%s len=%d",
                idx + 1,
                len(self.opening_parts),
                self.call_id,
                len(part),
            )
            try:
                await asyncio.wait_for(self._opening_tts_done.wait(), timeout=35.0)
            except asyncio.TimeoutError:
                LOG.warning(
                    "opening part %d TTS settle timeout call_id=%s",
                    idx + 1,
                    self.call_id,
                )
            if idx + 1 < len(self.opening_parts):
                await self._wait_opening_part_playback_drain(
                    idx + 1, breath_ms=160, wait_full_idle=True
                )
            else:
                await self._wait_opening_part_playback_drain(
                    idx + 1, breath_ms=0, wait_full_idle=True
                )
        self._accept_uplink = True
        self._opening_uplink_pending = False
        self._emit("audio_mode_ready")
        LOG.info("opening TTS all parts done call_id=%s; uplink enabled", self.call_id)

    async def _confirm_opening_tts_settled(self, token: int) -> None:
        """After tts_end: require TX idle + quiet window with no new audio/restart.

        Long SayHello may emit multiple audio started/done; first done must not
        finish the opening part while another stream is still coming.
        """
        if not self._opening_uplink_pending:
            return
        done = self._opening_tts_done
        if not done or done.is_set():
            return

        session = self._session
        pcm_bytes = self._opening_part_pcm24_bytes
        duration_sec = pcm_bytes / float(AGENT_TTS_RATE * 2) if pcm_bytes > 0 else 0.0
        tail = self._pcmu_tx_buf.flush_pad_silence()
        if tail and session:
            session.enqueue_pcmu(tail)

        if session:
            timeout = max(2.0, duration_sec + 3.0)
            await asyncio.to_thread(
                session.wait_downlink_idle,
                timeout_sec=timeout,
                quiet_frames=2,
            )

        # Quiet observe: abort if TTS restarts or more audio arrives.
        quiet_sec = 0.28
        deadline = time.monotonic() + quiet_sec
        pcm_mark = self._opening_part_pcm24_bytes
        while time.monotonic() < deadline:
            if token != self._opening_settle_token or not self._opening_uplink_pending:
                LOG.info(
                    "opening settle aborted call_id=%s token=%s→%s",
                    self.call_id,
                    token,
                    self._opening_settle_token,
                )
                return
            if self._opening_part_pcm24_bytes != pcm_mark:
                LOG.info(
                    "opening settle saw more audio call_id=%s; wait next tts_end",
                    self.call_id,
                )
                return
            await asyncio.sleep(0.05)

        if token != self._opening_settle_token or not self._opening_uplink_pending:
            return
        if done.is_set():
            return
        done.set()
        LOG.info(
            "opening TTS settled call_id=%s token=%s pcm24k=%d",
            self.call_id,
            token,
            self._opening_part_pcm24_bytes,
        )

    async def _wait_opening_part_playback_drain(
        self,
        part_idx: int,
        *,
        breath_ms: int = 160,
        wait_full_idle: bool = True,
    ) -> None:
        """Wait until RTP TX queue is empty before next SayHello / uplink.

        Do not use estimated TTS duration sleep: generation end ≠ phone playback end.
        Barge-in / normal turns still use clear_downlink — this path is drain only.
        """
        pcm_bytes = self._opening_part_pcm24_bytes
        duration_sec = pcm_bytes / float(AGENT_TTS_RATE * 2) if pcm_bytes > 0 else 0.0
        tail = self._pcmu_tx_buf.flush_pad_silence()
        session = self._session
        if tail and session:
            session.enqueue_pcmu(tail)

        pending = session.downlink_tx_queue_size() if session else 0
        t0 = time.monotonic()
        idle_ok = True
        if wait_full_idle and session:
            # Cap wait: audio duration + 3s slack (network / scheduler).
            timeout = max(2.0, duration_sec + 3.0)
            idle_ok = await asyncio.to_thread(
                session.wait_downlink_idle,
                timeout_sec=timeout,
                quiet_frames=2,
            )

        # Optional breath gap between parts (not used after last part).
        breath_frames = max(0, int(round(breath_ms / 20.0)))
        if breath_frames and session:
            session.enqueue_silence_frames(breath_frames)
            await asyncio.to_thread(
                session.wait_downlink_idle,
                timeout_sec=max(1.0, breath_ms / 1000.0 + 0.5),
                quiet_frames=1,
            )

        elapsed = time.monotonic() - t0
        LOG.info(
            "opening part %d TX idle call_id=%s pcm24k=%d est=%.2fs pending0=%d "
            "idle_ok=%s breath_ms=%d elapsed=%.2fs",
            part_idx,
            self.call_id,
            pcm_bytes,
            duration_sec,
            pending,
            idle_ok,
            breath_ms,
            elapsed,
        )
        if not idle_ok:
            LOG.warning(
                "opening part %d TX idle timeout call_id=%s; continuing",
                part_idx,
                self.call_id,
            )

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
        text = (text or "").strip()
        if not text:
            return
        if is_interim:
            self._asr_interim = text
            self._emit("asr_interim", text=text)
            return
        # Finals also come via on_asr_ended; avoid double-append.

    def _on_asr_ended(self, text: str) -> None:
        text = (text or "").strip()
        if not text:
            return
        self._latency.user_speech_end()
        self._pending_user_end_ms = self._rel_ms()
        self._asr_interim = ""
        self._transcript.append(
            {
                "role": "user",
                "text": text,
                "kind": "user",
                "source": "asr",
                "t_ms": self._pending_user_end_ms,
            }
        )
        self._publish_transcript()

    def _on_chat_delta(self, delta: str, _item_id: str, _reply_id: str) -> None:
        if delta:
            self._assistant_buf += delta

    def _on_chat_ended(self, _item_id: str, _reply_id: str) -> None:
        text = self._assistant_buf.strip()
        self._assistant_buf = ""
        if not text:
            return
        entry: dict[str, Any] = {
            "role": "assistant",
            "text": text,
            "kind": "reply",
            "source": "stt_chat",
            "t_ms": self._rel_ms(),
        }
        if self._pending_turn_latency_ms is not None:
            entry["latency_ms"] = int(round(self._pending_turn_latency_ms))
            self._pending_turn_latency_ms = None
        self._transcript.append(entry)
        self._publish_transcript()

    def _clear_downlink(self, reason: str) -> None:
        """Drop pending PCMU so a new AI utterance does not overlap stale TTS."""
        self._pcmu_tx_buf = PcmuFrameBuffer()
        dropped = 0
        if self._session:
            try:
                dropped = self._session.clear_downlink_audio()
            except Exception:
                LOG.debug("clear_downlink_audio failed", exc_info=True)
        if dropped:
            LOG.info(
                "cleared downlink TTS call_id=%s reason=%s dropped_frames=%d",
                self.call_id,
                reason,
                dropped,
            )

    def _on_barge_in(self, _item_id: str) -> None:
        # Phone echo / user speech while AI talking: Duplex restarts TTS —
        # must clear local RTP queue or old+new audio stacks (叠音).
        # Opening: uplink may be gated, but echo can still trigger restart → clear too.
        if self._opening_active or self._opening_uplink_pending:
            self._opening_settle_token += 1
            self._clear_downlink("opening_barge_in")
            self._opening_part_pcm24_bytes = 0
            self._opening_received_audio = False
            LOG.info("opening barge_in cleared downlink call_id=%s", self.call_id)
            return
        self._clear_downlink("barge_in")

    def _on_tts_start(self, _meta: dict) -> None:
        if self._opening_tts_started and not self._opening_tts_started.is_set():
            self._opening_tts_started.set()
        if self._opening_active or self._opening_uplink_pending:
            # Intra-opening restart / second audio stream: drop stale queue (防句内叠流).
            if self._opening_received_audio or self._opening_part_pcm24_bytes > 0:
                self._clear_downlink("opening_tts_restart")
                self._opening_part_pcm24_bytes = 0
                LOG.info(
                    "opening tts_start restart cleared downlink call_id=%s",
                    self.call_id,
                )
            self._opening_settle_token += 1
            self._opening_received_audio = False
            if self._opening_tts_start_ms is None:
                self._opening_tts_start_ms = self._rel_ms()
            return
        # New model utterance: start from a clean TX queue.
        self._clear_downlink("tts_start")
        # Normal turn: user finished → AI TTS starts = SIP turn latency.
        self._latency.tts_start()
        lat = self._latency.get_last_total_latency_ms()
        if lat is not None:
            self._pending_turn_latency_ms = lat
            self._turn_timings.append(
                {
                    "kind": "turn",
                    "user_speech_end_ms": self._pending_user_end_ms,
                    "ai_speech_start_ms": self._rel_ms(),
                    "latency_ms": int(round(lat)),
                }
            )
            self._emit(
                "turn_latency",
                latency_ms=int(round(lat)),
                turn_timings=list(self._turn_timings),
            )
            LOG.info(
                "SIP turn latency call_id=%s latency_ms=%.0f",
                self.call_id,
                lat,
            )

    def _on_tts_end(self, _meta: dict) -> None:
        if self._opening_uplink_pending and self._opening_tts_done and not self._opening_tts_done.is_set():
            token = self._opening_settle_token
            loop = self._loop
            if loop and loop.is_running():
                loop.create_task(self._confirm_opening_tts_settled(token))
                LOG.info(
                    "opening TTS end → settle confirm call_id=%s token=%s",
                    self.call_id,
                    token,
                )
            else:
                self._opening_tts_done.set()
                LOG.info("opening TTS ended (sync) call_id=%s", self.call_id)
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
        if self._opening_uplink_pending:
            self._opening_part_pcm24_bytes += len(pcm24k)
            self._opening_received_audio = True
        if self._opening_tts_started and not self._opening_tts_started.is_set():
            self._opening_tts_started.set()
        if (self._opening_active or self._opening_uplink_pending) and self._opening_tts_start_ms is None:
            self._opening_tts_start_ms = self._rel_ms()
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
