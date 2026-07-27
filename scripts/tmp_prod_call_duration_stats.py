#!/usr/bin/env python3
"""统计 tmp-prod 已有通话的平均时长（基于落盘 WAV）。

优先读::

    {root}/recordings/{call_id}/stereo.wav

若 recordings 已清理，再回退到批次对齐拷贝::

    {root}/{batch_id}/contacts/{contact_id}/{call_id}/stereo.wav

按 call_id 去重，避免同一通被算两次。无 WAV 时可用 transcript 里的 max(t_ms)。

Usage::

    python scripts/tmp_prod_call_duration_stats.py
    python scripts/tmp_prod_call_duration_stats.py --root /home/ubuntu/realtime_dialogue_demo/data/tmp_prod
    python scripts/tmp_prod_call_duration_stats.py --root data/tmp_prod --list --min-sec 5
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import wave
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class CallDuration:
    call_id: str
    seconds: float
    source: str  # stereo.wav | user.wav | transcript
    path: str


def _wav_duration_sec(path: Path) -> float | None:
    try:
        with wave.open(str(path), "rb") as wf:
            frames = wf.getnframes()
            rate = wf.getframerate()
            if rate <= 0:
                return None
            return frames / float(rate)
    except Exception:
        return None


def _transcript_duration_sec(path: Path) -> float | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    turns = data.get("transcript") if isinstance(data, dict) else data
    if not isinstance(turns, list):
        return None
    max_ms = 0
    for t in turns:
        if not isinstance(t, dict):
            continue
        for key in ("t_ms", "user_speech_end_ms", "ai_speech_start_ms"):
            v = t.get(key)
            if isinstance(v, (int, float)) and v > max_ms:
                max_ms = int(v)
    if max_ms <= 0:
        return None
    return max_ms / 1000.0


def _pick_wav(call_dir: Path) -> tuple[Path, str] | None:
    for name in ("stereo.wav", "user.wav", "mixed.wav", "ai.wav"):
        p = call_dir / name
        if p.is_file():
            return p, name
    return None


def collect_from_recordings(root: Path) -> dict[str, CallDuration]:
    out: dict[str, CallDuration] = {}
    rec_root = root / "recordings"
    if not rec_root.is_dir():
        return out
    for call_dir in sorted(rec_root.iterdir()):
        if not call_dir.is_dir():
            continue
        picked = _pick_wav(call_dir)
        if not picked:
            continue
        path, name = picked
        sec = _wav_duration_sec(path)
        if sec is None:
            continue
        out[call_dir.name] = CallDuration(
            call_id=call_dir.name,
            seconds=sec,
            source=name,
            path=str(path),
        )
    return out


def collect_from_contacts(root: Path, existing: dict[str, CallDuration]) -> dict[str, CallDuration]:
    """Fill gaps from batch contact copies; skip call_ids already found."""
    out = dict(existing)
    for batch_dir in sorted(root.iterdir()):
        contacts = batch_dir / "contacts"
        if not contacts.is_dir():
            continue
        for call_dir in contacts.glob("*/*/"):
            if not call_dir.is_dir():
                continue
            call_id = call_dir.name
            if call_id in out:
                continue
            picked = _pick_wav(call_dir)
            if not picked:
                continue
            path, name = picked
            sec = _wav_duration_sec(path)
            if sec is None:
                continue
            out[call_id] = CallDuration(
                call_id=call_id,
                seconds=sec,
                source=f"contacts/{name}",
                path=str(path),
            )
    return out


def collect_from_transcripts(root: Path, existing: dict[str, CallDuration]) -> dict[str, CallDuration]:
    out = dict(existing)
    for path in root.glob("*/artifacts/*/*_transcript.json"):
        call_id = path.name.removesuffix("_transcript.json")
        if call_id in out:
            continue
        sec = _transcript_duration_sec(path)
        if sec is None:
            continue
        out[call_id] = CallDuration(
            call_id=call_id,
            seconds=sec,
            source="transcript",
            path=str(path),
        )
    return out


def _fmt_sec(sec: float) -> str:
    if sec < 60:
        return f"{sec:.1f}s"
    m = int(sec // 60)
    s = sec - m * 60
    return f"{m}m{s:04.1f}s"


def _percentile(sorted_vals: list[float], p: float) -> float:
    if not sorted_vals:
        return 0.0
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    k = (len(sorted_vals) - 1) * (p / 100.0)
    f = int(k)
    c = min(f + 1, len(sorted_vals) - 1)
    if f == c:
        return sorted_vals[f]
    return sorted_vals[f] + (sorted_vals[c] - sorted_vals[f]) * (k - f)


def print_report(calls: list[CallDuration], *, list_calls: bool) -> None:
    if not calls:
        print("未找到任何可统计的通话录音/转写。")
        return

    secs = [c.seconds for c in calls]
    secs_sorted = sorted(secs)
    print(f"通话数:        {len(calls)}")
    print(f"平均时长:      {_fmt_sec(statistics.mean(secs))}  ({statistics.mean(secs):.1f}s)")
    print(f"中位数:        {_fmt_sec(statistics.median(secs))}  ({statistics.median(secs):.1f}s)")
    print(f"最短:          {_fmt_sec(min(secs))}")
    print(f"最长:          {_fmt_sec(max(secs))}")
    print(f"P90:           {_fmt_sec(_percentile(secs_sorted, 90))}")
    print(f"总时长:        {_fmt_sec(sum(secs))}")
    if len(secs) >= 2:
        print(f"标准差:        {statistics.stdev(secs):.1f}s")

    # Simple histogram buckets
    buckets = [
        (0, 30, "0–30s"),
        (30, 60, "30–60s"),
        (60, 120, "1–2min"),
        (120, 300, "2–5min"),
        (300, 600, "5–10min"),
        (600, 900, "10–15min"),
        (900, float("inf"), "≥15min"),
    ]
    print("\n时长分布:")
    for lo, hi, label in buckets:
        n = sum(1 for s in secs if lo <= s < hi)
        if n == 0:
            continue
        bar = "█" * max(1, round(n * 40 / len(secs)))
        print(f"  {label:<10} {n:>4}  {bar}")

    by_src: dict[str, int] = {}
    for c in calls:
        by_src[c.source] = by_src.get(c.source, 0) + 1
    print("\n数据来源:")
    for src, n in sorted(by_src.items(), key=lambda x: -x[1]):
        print(f"  {src}: {n}")

    if list_calls:
        print("\n明细 (按时长降序):")
        for c in sorted(calls, key=lambda x: -x.seconds):
            print(f"  {_fmt_sec(c.seconds):>10}  {c.call_id}  [{c.source}]")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="统计 tmp-prod 每通通话平均时长（基于 WAV / transcript）",
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=None,
        help="tmp-prod 数据根目录（默认: $TMP_PROD_ROOT 或 data/tmp_prod）",
    )
    parser.add_argument(
        "--min-sec",
        type=float,
        default=0.0,
        help="过滤短于该秒数的通话（默认 0，不过滤）",
    )
    parser.add_argument(
        "--max-sec",
        type=float,
        default=None,
        help="过滤长于该秒数的通话",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="列出每通通话时长",
    )
    parser.add_argument(
        "--include-transcript-fallback",
        action="store_true",
        help="无 WAV 时用 transcript max(t_ms) 补全",
    )
    args = parser.parse_args(argv)

    import os

    root = args.root
    if root is None:
        root = Path(os.environ.get("TMP_PROD_ROOT", "data/tmp_prod"))
    root = root.expanduser().resolve()

    if not root.is_dir():
        print(f"目录不存在: {root}", file=sys.stderr)
        return 1

    print(f"扫描目录: {root}")
    calls_map = collect_from_recordings(root)
    calls_map = collect_from_contacts(root, calls_map)
    if args.include_transcript_fallback:
        calls_map = collect_from_transcripts(root, calls_map)

    calls = list(calls_map.values())
    if args.min_sec > 0:
        calls = [c for c in calls if c.seconds >= args.min_sec]
    if args.max_sec is not None:
        calls = [c for c in calls if c.seconds <= args.max_sec]

    print_report(calls, list_calls=args.list)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
