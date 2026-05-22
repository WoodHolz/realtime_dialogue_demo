#!/usr/bin/env python3
"""
Measure Duplug VAD latency: idle → nonidle (server-side "speech start").

中文：先上行静音直到出现 idle，再突然改喂有声 PCM，统计从「开始喂声」到首条 nonidle 的耗时（ms）。
English: prime with silence until `idle`, then feed tone; time until first `nonidle` turn_state.

Usage (from repo root, with venv that has websockets):
  python scripts/measure_duplug_idle_to_nonidle.py
  DUPLUG_VAD_URL=ws://127.0.0.1:7997/turn python scripts/measure_duplug_idle_to_nonidle.py --runs 5

Requires the same SoulX-Duplug WebSocket as backend (see backend/duplug_vad_client.py).
"""

from __future__ import annotations

import argparse
import asyncio
import math
import os
import statistics
import struct
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.duplug_vad_client import DuplugVadSession, _load_duplug_vad_config  # noqa: E402


def _sine_s16le_chunk(num_samples: int, *, sr: int, freq_hz: float, amp: float) -> bytes:
    out = bytearray(num_samples * 2)
    for i in range(num_samples):
        s = int(32767 * amp * math.sin(2.0 * math.pi * freq_hz * (i / float(sr))))
        s = max(-32768, min(32767, s))
        struct.pack_into("<h", out, i * 2, s)
    return bytes(out)


async def _feed_until_state(
    sess: DuplugVadSession,
    pcm: bytes,
    *,
    want: str,
    max_seconds: float,
) -> bool:
    deadline = time.perf_counter() + max_seconds
    while time.perf_counter() < deadline:
        st = await sess.feed_pcm_s16le_16k(pcm)
        if st is not None and st.state == want:
            return True
    return False


async def measure_idle_to_nonidle_ms(
    server_url: str,
    *,
    silence_seconds: float,
    idle_wait_s: float,
    speech_wait_s: float,
    tone_hz: float,
    tone_amp: float,
    chunk_samples: int,
) -> float | None:
    sess = DuplugVadSession(server_url=server_url, chunk_samples=chunk_samples)
    try:
        await sess.connect()
        silence = bytes(chunk_samples * 2)
        speech = _sine_s16le_chunk(chunk_samples, sr=16000, freq_hz=tone_hz, amp=tone_amp)

        # Prime: silence to settle pipeline
        t_prime = time.perf_counter() + silence_seconds
        while time.perf_counter() < t_prime:
            await sess.feed_pcm_s16le_16k(silence)

        # Wait until we observe idle (so transition is meaningful)
        if not await _feed_until_state(sess, silence, want="idle", max_seconds=idle_wait_s):
            return None

        # First speech chunk time → first nonidle
        t0 = time.perf_counter()
        deadline = t0 + speech_wait_s
        while time.perf_counter() < deadline:
            st = await sess.feed_pcm_s16le_16k(speech)
            if st is not None and st.state == "nonidle":
                return (time.perf_counter() - t0) * 1000.0

        return None
    finally:
        await sess.close()


async def _amain() -> int:
    ap = argparse.ArgumentParser(description="Duplug idle→nonidle latency probe")
    ap.add_argument("--runs", type=int, default=3, help="number of cold measurements")
    ap.add_argument("--prime-s", type=float, default=0.4, help="initial silence seconds before idle wait")
    ap.add_argument("--idle-wait-s", type=float, default=5.0, help="max wait for first idle")
    ap.add_argument("--speech-wait-s", type=float, default=8.0, help="max wait for nonidle after speech starts")
    ap.add_argument("--tone-hz", type=float, default=440.0)
    ap.add_argument("--tone-amp", type=float, default=0.25, help="0..1 sine amplitude (peak)")
    ap.add_argument("--chunk-samples", type=int, default=2560, help="must match Duplug chunking (default 2560)")
    ap.add_argument("--url", type=str, default="", help="override Duplug WS URL (else env/config)")
    args = ap.parse_args()

    _enabled, cfg_url = _load_duplug_vad_config()
    url = (args.url or os.environ.get("DUPLUG_VAD_URL", "") or "").strip() or cfg_url
    if not url:
        print("No Duplug URL: set DUPLUG_VAD_URL or config.json vad.duplug_url", file=sys.stderr)
        return 2

    print(f"Duplug URL: {url}")
    print(
        f"runs={args.runs} prime_s={args.prime_s} idle_wait_s={args.idle_wait_s} "
        f"speech_wait_s={args.speech_wait_s} chunk_samples={args.chunk_samples} tone={args.tone_hz}Hz amp={args.tone_amp}"
    )

    results: list[float] = []
    for i in range(args.runs):
        ms = await measure_idle_to_nonidle_ms(
            url,
            silence_seconds=args.prime_s,
            idle_wait_s=args.idle_wait_s,
            speech_wait_s=args.speech_wait_s,
            tone_hz=args.tone_hz,
            tone_amp=args.tone_amp,
            chunk_samples=args.chunk_samples,
        )
        if ms is None:
            print(f"run {i + 1}: FAIL (no idle or no nonidle within timeout)")
        else:
            print(f"run {i + 1}: idle→nonidle = {ms:.1f} ms")
            results.append(ms)
        await asyncio.sleep(0.15)

    if not results:
        print("No successful runs.", file=sys.stderr)
        return 1
    if len(results) == 1:
        print(f"mean = {results[0]:.1f} ms")
    else:
        print(
            f"mean = {statistics.mean(results):.1f} ms  "
            f"stdev = {statistics.stdev(results):.1f} ms  "
            f"min/max = {min(results):.1f} / {max(results):.1f} ms"
        )
    return 0


def main() -> None:
    raise SystemExit(asyncio.run(_amain()))


if __name__ == "__main__":
    main()
