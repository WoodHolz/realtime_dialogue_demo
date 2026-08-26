# -*- coding: utf-8 -*-
"""Turn orchestrator: speculative LLM with cancel-restart, TTS only after aligned EoU."""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import Awaitable, Callable

from backend.pipeline.dialogue import DialogueState, HistoryTurn, system_with_progress
from backend.pipeline.protocols import StreamingAsr, StreamingLlm, StreamingTts
from backend.pipeline.textutil import SentenceBuffer, normalize_user_text, user_prompts_aligned
from config.tts_script_prompt import with_tts_script_requirements

LOG = logging.getLogger(__name__)

EmitJson = Callable[[dict], None]
EmitPcm = Callable[[bytes], Awaitable[None] | None]
AsrFactory = Callable[[], StreamingAsr]


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


class PipelineOrchestrator:
    def __init__(
        self,
        *,
        llm: StreamingLlm,
        tts: StreamingTts,
        asr_factory: AsrFactory | None = None,
        asr: StreamingAsr | None = None,
        emit_json: EmitJson | None = None,
        emit_pcm: EmitPcm | None = None,
        system_role: str = "",
        voice: str = "",
        speculate_debounce_sec: float = 0.2,
        tts_barge_suppress_ms: int = 400,
    ) -> None:
        self.llm = llm
        self.tts = tts
        self._asr_factory = asr_factory
        self._asr: StreamingAsr | None = asr
        self._owns_injected_asr = asr is not None
        self.emit_json = emit_json or (lambda _e: None)
        self.emit_pcm = emit_pcm
        self.system_role = system_role
        self.voice = voice
        self.speculate_debounce_sec = speculate_debounce_sec
        self.tts_barge_suppress_ms = tts_barge_suppress_ms
        self.state = DialogueState()
        self._sentences = SentenceBuffer()
        self._queued: list[str] = []
        self._speculate_task: asyncio.Task | None = None
        self._tts_started = False
        self._llm_done = False
        self._first_audio_mono = 0.0
        self._spoken_sentences: list[str] = []
        self._audio_for_index = 0
        self._closed = False
        self._pending_snapshot = ""
        self._queued_spoken_candidate = ""
        self._flush_scheduled = False
        self._flush_task: asyncio.Task | None = None
        self._turn_committed = False
        self._downlink_muted = True
        self._client_may_have_tts = False
        self._tts_cancel_task: asyncio.Task | None = None
        self._bind_llm_tts()

    def _bind_llm_tts(self) -> None:
        bind = getattr(self.llm, "bind", None)
        if callable(bind):
            bind(on_delta=self._on_llm_delta, on_sentence=self._on_llm_sentence, on_done=self._on_llm_done)
        bind_t = getattr(self.tts, "bind", None)
        if callable(bind_t):
            bind_t(on_audio=self._on_tts_audio, on_start=self._on_tts_start, on_end=self._on_tts_end)

    def tts_barge_suppress_active(self) -> bool:
        """Echo lead-in only while server is still in tts_playing."""
        if self._downlink_muted or self.tts_barge_suppress_ms <= 0 or self._first_audio_mono <= 0:
            return False
        if self.state.phase != "tts_playing":
            return False
        elapsed_ms = (time.monotonic() - self._first_audio_mono) * 1000.0
        return elapsed_ms < float(self.tts_barge_suppress_ms)

    def set_system_role(self, system_role: str) -> None:
        self.system_role = system_role or self.system_role

    def _emit(self, payload: dict) -> None:
        try:
            self.emit_json(payload)
        except Exception:
            LOG.debug("pipeline emit_json failed", exc_info=True)

    def _emit_turn(self, phase: str) -> None:
        self._emit(
            {
                "type": "pipeline_turn",
                "turn_id": self.state.turn_id,
                "gen_id": self.state.gen_id,
                "phase": phase,
            }
        )

    async def close(self) -> None:
        self._closed = True
        self._cancel_debounce()
        await self._cancel_llm()
        await self.tts.cancel()
        if self._asr:
            await self._asr.cancel()
            if not self._owns_injected_asr:
                self._asr = None

    async def start_uplink(self) -> None:
        """Warm ASR for a new capture lane (voice_input_start)."""
        if self._asr and not self._owns_injected_asr:
            await self._asr.cancel()
            self._asr = None
        if self._asr_factory:
            self._asr = self._asr_factory()
            bind = getattr(self._asr, "bind", None)
            if callable(bind):
                bind(on_partial=self.on_asr_partial)
        if self._asr:
            await self._asr.start(turn_id=self.state.turn_id or _new_id("turn"))

    async def feed_pcm(self, pcm16_16k: bytes) -> None:
        if self._asr and pcm16_16k:
            await self._asr.send_pcm(pcm16_16k, is_last=False)

    async def abort_uplink(self) -> None:
        if self._asr:
            await self._asr.cancel()
            if not self._owns_injected_asr:
                self._asr = None

    async def on_speech_start(self, *, source: str = "vad") -> bool:
        """Open a user turn. Real barge-in only while TTS is still active."""
        barged = False
        actively_playing = self.state.phase == "tts_playing" or self._tts_started
        if actively_playing and not self.tts_barge_suppress_active():
            await self._barge_in(source=source)
            barged = True
        elif self._client_may_have_tts and self.state.phase == "idle":
            # Server already finished; leftover browser queue is not a user interrupt.
            await self._clear_leftover_playback(source=source)
        if self.state.phase in {"idle", "committed"} or barged:
            self._begin_user_turn()
        return barged

    async def on_asr_partial(self, text: str, is_final: bool = False) -> None:
        snapshot = (text or "").strip()
        if not snapshot:
            return
        self.state.user_snapshot = snapshot
        self._emit({"type": "asr", "text": snapshot, "is_interim": not is_final})
        if is_final:
            return
        if self.state.phase != "user_speaking":
            return
        if user_prompts_aligned(self.state.prompt_user, snapshot) and self.state.gen_id:
            return
        self._schedule_speculate(snapshot)

    async def on_eou(self, final_text: str | None = None) -> None:
        self._cancel_debounce()
        final = (final_text or "").strip()
        if not final and self._asr:
            try:
                await self._asr.send_pcm(b"", is_last=True)
                final = (await self._asr.wait_final() or "").strip()
            except Exception:
                LOG.warning("pipeline ASR finalize failed", exc_info=True)
            if not self._owns_injected_asr:
                self._asr = None
        if not final:
            final = (self.state.user_snapshot or "").strip()
        self.state.user_snapshot = final
        self._emit({"type": "asr_ended", "final_text": final})
        if not final:
            self._emit({"type": "voice_turn_finished", "asr_text_sent": False})
            self.state.phase = "idle"
            return
        # speech_start can be skipped (TTS barge suppress / VAD miss). If we
        # overwrite pending_user while still committed/tts_playing, history stays
        # empty and the next LLM recites the opening again.
        if self.state.phase in {"committed", "tts_playing"}:
            if self.state.phase == "tts_playing" or self._tts_started:
                await self._barge_in(source="eou")
            elif self._client_may_have_tts:
                await self._clear_leftover_playback(source="eou")
                await self._cancel_llm()
            else:
                await self._cancel_llm()
            self._begin_user_turn()
        elif self.state.phase == "idle":
            if self._client_may_have_tts:
                await self._clear_leftover_playback(source="eou")
            self._begin_user_turn()
        self._emit({"type": "local_asr_text", "text": final})
        self.state.pending_user = final
        await self._commit_or_restart(final)

    async def on_text_query(self, text: str) -> None:
        self._cancel_debounce()
        final = (text or "").strip()
        if not final:
            return
        if self.state.phase == "tts_playing" or self._tts_started:
            await self._barge_in(source="text")
        elif self._client_may_have_tts:
            await self._clear_leftover_playback(source="text")
        self._begin_user_turn()
        self.state.user_snapshot = final
        self._emit({"type": "local_asr_text", "text": final})
        self.state.pending_user = final
        await self._commit_or_restart(final)
        self._emit({"type": "chat_text_query_sent", "reason": "pipeline_text"})

    def _begin_user_turn(self) -> None:
        # Must land the previous user+assistant before wiping draft, or the
        # next LLM call only sees system_role + new user text and repeats Q0.
        self._finalize_current_turn()
        self.state.turn_id = _new_id("turn")
        self.state.user_snapshot = ""
        self.state.prompt_user = ""
        self.state.gen_id = ""
        self.state.draft = ""
        self.state.spoken_text = ""
        self.state.phase = "user_speaking"
        self._queued.clear()
        self._sentences.clear()
        self._spoken_sentences.clear()
        self._audio_for_index = 0
        self._tts_started = False
        self._llm_done = False
        self._first_audio_mono = 0.0
        self._queued_spoken_candidate = ""
        self._pending_snapshot = ""
        self._flush_scheduled = False
        self._flush_task = None
        self._turn_committed = False
        self._downlink_muted = True
        self._client_may_have_tts = False
        self._emit_turn("speculative")

    def _schedule_speculate(self, snapshot: str) -> None:
        self._pending_snapshot = snapshot
        self._cancel_debounce()
        self._speculate_task = asyncio.create_task(self._debounced_speculate())

    async def _debounced_speculate(self) -> None:
        try:
            await asyncio.sleep(self.speculate_debounce_sec)
        except asyncio.CancelledError:
            return
        if self._closed or self.state.phase != "user_speaking":
            return
        snapshot = getattr(self, "_pending_snapshot", "")
        if not snapshot:
            return
        if user_prompts_aligned(self.state.prompt_user, snapshot) and self.state.gen_id:
            return
        await self._restart_llm(snapshot, reason="revise")

    def _cancel_debounce(self) -> None:
        task = self._speculate_task
        self._speculate_task = None
        if task and not task.done():
            task.cancel()

    async def _restart_llm(self, user_text: str, *, reason: str) -> None:
        old = self.state.gen_id
        if old:
            await self.llm.cancel(old)
            self._emit({"type": "llm_revised", "turn_id": self.state.turn_id, "gen_id": old, "user_text": user_text})
        self._sentences.clear()
        self._queued.clear()
        self.state.draft = ""
        self._llm_done = False
        gen_id = _new_id("gen")
        self.state.gen_id = gen_id
        self.state.prompt_user = user_text
        self._emit(
            {
                "type": "llm_speculative_start",
                "turn_id": self.state.turn_id,
                "gen_id": gen_id,
                "user_text": user_text,
                "reason": reason,
                "history_turns": len(self.state.history),
            }
        )
        LOG.info(
            "pipeline llm start gen_id=%s reason=%s hist=%s user=%s",
            gen_id,
            reason,
            len(self.state.history),
            (user_text or "")[:40],
        )
        # Base interview prompt stays shared; TTS manuscript rules only join on pipeline LLM calls.
        await self.llm.start_speculative(
            system=system_with_progress(
                with_tts_script_requirements(self.system_role),
                self.state.history,
            ),
            history=self.state.committed_history(),
            user_text=user_text,
            gen_id=gen_id,
        )

    async def _commit_or_restart(self, final: str) -> None:
        aligned = bool(self.state.gen_id) and user_prompts_aligned(self.state.prompt_user, final)
        if aligned:
            self.state.phase = "committed"
            await self.llm.commit(final_text=final, gen_id=self.state.gen_id)
            self._emit(
                {
                    "type": "llm_committed",
                    "turn_id": self.state.turn_id,
                    "gen_id": self.state.gen_id,
                    "user_text": final,
                }
            )
            self._emit_turn("committed")
            await self._drain_tts()
            return
        if self.state.gen_id:
            self._emit_turn("discarded")
        await self._restart_llm(final, reason="eou_mismatch")
        self.state.phase = "committed"
        await self.llm.commit(final_text=final, gen_id=self.state.gen_id)
        self._emit(
            {
                "type": "llm_committed",
                "turn_id": self.state.turn_id,
                "gen_id": self.state.gen_id,
                "user_text": final,
            }
        )
        self._emit_turn("committed")
        await self._drain_tts()

    async def _drain_tts(self) -> None:
        if self._tts_started:
            return
        if self.state.phase not in {"committed", "tts_playing"}:
            return
        ready = list(self._queued)
        if not ready and self._llm_done:
            leftover = self._sentences.flush()
            if leftover:
                ready.append(leftover)
        if not ready:
            return
        await self._start_tts()
        if not self._tts_started:
            return
        self._queued.clear()
        for sent in ready:
            await self._feed_tts_sentence(sent)
        if self._llm_done:
            leftover = self._sentences.flush()
            if leftover:
                await self._feed_tts_sentence(leftover)
            self._schedule_flush()

    async def _start_tts(self) -> None:
        if self._tts_started:
            return
        prev_phase = self.state.phase
        self.state.phase = "tts_playing"
        self._first_audio_mono = 0.0
        self._downlink_muted = False
        try:
            await self.tts.start(voice=self.voice, sample_rate=24000, gen_id=self.state.gen_id)
            self._tts_started = True
        except Exception:
            LOG.warning("pipeline TTS start failed gen_id=%s", self.state.gen_id, exc_info=True)
            self._tts_started = False
            self.state.phase = prev_phase if prev_phase in {"committed", "tts_playing"} else "committed"
            self._emit(
                {
                    "type": "pipeline_error",
                    "where": "tts_start",
                    "turn_id": self.state.turn_id,
                    "gen_id": self.state.gen_id,
                }
            )
            # Keep draft so the next user turn can still commit assistant text.

    async def _feed_tts_sentence(self, sentence: str) -> None:
        if not sentence or not self._tts_started:
            return
        self._queued_spoken_candidate = sentence
        try:
            await self.tts.feed(sentence)
        except Exception:
            # DashScope adapter should not raise; keep LLM stream alive regardless.
            LOG.warning("pipeline TTS feed failed gen_id=%s", self.state.gen_id, exc_info=True)

    async def _on_llm_delta(self, gen_id: str, chunk: str) -> None:
        if gen_id != self.state.gen_id or self._closed:
            return
        self.state.draft += chunk
        self._emit({"type": "chat", "content": chunk, "reply_id": gen_id, "question_id": self.state.turn_id})
        for sent in self._sentences.feed(chunk):
            try:
                await self._on_llm_sentence(gen_id, sent)
            except Exception:
                LOG.warning("pipeline sentence→TTS path failed gen_id=%s", gen_id, exc_info=True)

    async def _on_llm_sentence(self, gen_id: str, sentence: str) -> None:
        if gen_id != self.state.gen_id or self._closed:
            return
        if self.state.phase in {"committed", "tts_playing"}:
            if not self._tts_started:
                self._queued.append(sentence)
                await self._drain_tts()
            else:
                await self._feed_tts_sentence(sentence)
        else:
            self._queued.append(sentence)

    async def _on_llm_done(self, gen_id: str) -> None:
        if gen_id != self.state.gen_id or self._closed:
            return
        self._llm_done = True
        leftover = self._sentences.flush()
        if leftover:
            if self.state.phase in {"committed", "tts_playing"}:
                if not self._tts_started:
                    self._queued.append(leftover)
                    await self._drain_tts()
                else:
                    await self._feed_tts_sentence(leftover)
            else:
                self._queued.append(leftover)
        elif self.state.phase in {"committed", "tts_playing"}:
            await self._drain_tts()
        if self._tts_started:
            self._schedule_flush()
        self._emit({"type": "chat_end", "question_id": self.state.turn_id, "reply_id": gen_id})

    def _schedule_flush(self) -> None:
        if self._flush_scheduled or not self._tts_started or self._downlink_muted:
            return
        self._flush_scheduled = True
        self._flush_task = asyncio.create_task(self._safe_flush(), name="pipeline-tts-flush")

    async def _safe_flush(self) -> None:
        if self._downlink_muted or self._closed:
            return
        gen = self.state.gen_id
        try:
            await self.tts.flush()
        except asyncio.CancelledError:
            raise
        except Exception:
            LOG.debug("pipeline tts flush failed", exc_info=True)
        if gen != self.state.gen_id or self._downlink_muted:
            return

    async def _on_tts_start(self) -> None:
        self._emit({"type": "tts_start", "question_id": self.state.turn_id, "reply_id": self.state.gen_id})

    async def _on_tts_audio(self, pcm: bytes) -> None:
        if self._downlink_muted or self.state.phase != "tts_playing" or self._closed:
            return
        if pcm:
            if self._first_audio_mono <= 0:
                self._first_audio_mono = time.monotonic()
            pending = self._queued_spoken_candidate
            if pending and (not self._spoken_sentences or self._spoken_sentences[-1] != pending):
                self._spoken_sentences.append(pending)
                self.state.spoken_text = "".join(self._spoken_sentences)
        if pcm and self.emit_pcm:
            self._client_may_have_tts = True
            result = self.emit_pcm(pcm)
            if asyncio.iscoroutine(result):
                await result

    async def _on_tts_end(self) -> None:
        if self.state.phase != "tts_playing":
            return
        self._finalize_current_turn()
        self.state.phase = "idle"
        self._tts_started = False
        # Keep _client_may_have_tts so a following utterance can silently clear
        # leftover browser queue without being labeled barge-in.
        self._emit({"type": "tts_end", "question_id": self.state.turn_id, "reply_id": self.state.gen_id})

    async def _clear_leftover_playback(self, *, source: str) -> None:
        """Stop browser leftover PCM after server tts_end without labeling barge-in."""
        if not self._client_may_have_tts:
            return
        self._downlink_muted = True
        self._client_may_have_tts = False
        self._emit(
            {
                "type": "tts_cancel",
                "turn_id": self.state.turn_id,
                "gen_id": self.state.gen_id,
                "source": source,
                "reason": "leftover_clear",
            }
        )

    async def _barge_in(self, *, source: str) -> None:
        """Mute + notify browser first; remote cancel runs in the background."""
        was_playing = self.state.phase == "tts_playing" or self._tts_started
        self._downlink_muted = True
        self._client_may_have_tts = False
        flush_task = self._flush_task
        self._flush_task = None
        self._flush_scheduled = False
        if flush_task and not flush_task.done():
            flush_task.cancel()
        self._finalize_current_turn()
        gen_id = self.state.gen_id
        if self.state.phase == "tts_playing":
            self.state.phase = "idle"
        self._tts_started = False
        self._first_audio_mono = 0.0
        if was_playing:
            self._emit(
                {
                    "type": "barge_in",
                    "source": source,
                    "reason": "user_speech",
                    "reason_label": "用户打断（检测到说话）",
                }
            )
            self._emit(
                {
                    "type": "tts_cancel",
                    "turn_id": self.state.turn_id,
                    "gen_id": gen_id,
                    "source": source,
                }
            )
            self._emit_turn("barge_in")
        self._schedule_cancel_tts()
        if gen_id:
            asyncio.create_task(self._cancel_llm_gen(gen_id), name="pipeline-llm-cancel")

    def _schedule_cancel_tts(self) -> None:
        task = self._tts_cancel_task
        if task and not task.done():
            return
        self._tts_cancel_task = asyncio.create_task(self._safe_cancel_tts(), name="pipeline-tts-cancel")

    async def _safe_cancel_tts(self) -> None:
        try:
            await self.tts.cancel()
        except Exception:
            LOG.debug("pipeline tts cancel failed", exc_info=True)

    async def _cancel_llm_gen(self, gen_id: str) -> None:
        try:
            await self.llm.cancel(gen_id)
        except Exception:
            LOG.debug("llm cancel failed", exc_info=True)

    def _assistant_text_for_history(self) -> str:
        spoken = (self.state.spoken_text or "".join(self._spoken_sentences)).strip()
        if spoken:
            return spoken
        return (self.state.draft or "").strip()

    def _finalize_current_turn(self) -> None:
        """Persist this turn before the next LLM call. Spoken PCM wins; else draft.

        History used to wait for TTS on_end only. If TTS never started/ended, the
        next start_speculative saw empty history and replayed the opening script.
        """
        if self._turn_committed:
            return
        user = (self.state.pending_user or "").strip()
        assistant = self._assistant_text_for_history()
        if not user and not assistant:
            return
        self._finalize_history(assistant)
        self._turn_committed = True

    def _finalize_history(self, spoken: str) -> None:
        user = (self.state.pending_user or "").strip()
        if user:
            self.state.history.append(HistoryTurn(role="user", text=user))
            self.state.pending_user = ""
        spoken = (spoken or "").strip()
        if spoken:
            self.state.history.append(HistoryTurn(role="assistant", text=spoken))
        self.state.spoken_text = spoken

    async def _cancel_llm(self) -> None:
        gen_id = self.state.gen_id
        if gen_id:
            try:
                await self.llm.cancel(gen_id)
            except Exception:
                LOG.debug("llm cancel failed", exc_info=True)
