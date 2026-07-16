#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
最小 SIP 外呼 + RTP(PCMU) 收发 + BYE 处理（联调 CLI）。

用法（仓库根目录）:
  python3 scripts/sip_min_call.py --called 18324133080
  python3 scripts/sip_min_call.py --called a118324133080 --duration 30

媒体层已抽到 backend/sip/；本脚本仅做静音保活联调（不含 Voice Agent）。
完整 AI 通话请走 POST /api/v1/calls（TELEPHONY_MODE=sip）。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Allow `python3 scripts/sip_min_call.py` from repo root without installing package.
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from backend.sip.config import SipConfig
from backend.sip.session import SipCallSession


def main() -> None:
    ap = argparse.ArgumentParser(description="Minimal SIP INVITE/ACK + RTP + BYE")
    ap.add_argument("--called", required=True, help="被叫手机号，可带或不带 a1 前缀")
    ap.add_argument("--prefix", default="a1")
    ap.add_argument("--dst", default="124.70.154.200")
    ap.add_argument("--dst-port", type=int, default=5060)
    ap.add_argument(
        "--src-ip",
        default="1.13.182.189",
        help="写入 SDP/Contact 的公网 IP（容联看到的媒体地址）",
    )
    ap.add_argument(
        "--bind-ip",
        default="0.0.0.0",
        help="本机 RTP socket 绑定地址，云主机一般用 0.0.0.0",
    )
    ap.add_argument("--src-port", type=int, default=5070)
    ap.add_argument("--rtp-port", type=int, default=10000)
    ap.add_argument("--from-user", default="01080435740")
    ap.add_argument("--ring-timeout", type=float, default=45.0, help="等 200 OK 秒数")
    ap.add_argument("--duration", type=float, default=20.0, help="接通后保持秒数")
    args = ap.parse_args()

    cfg = SipConfig(
        dst_host=args.dst,
        dst_port=args.dst_port,
        src_ip=args.src_ip,
        bind_ip=args.bind_ip,
        src_sip_port=args.src_port,
        rtp_port=args.rtp_port,
        from_user=args.from_user,
        called_prefix=args.prefix,
        ring_timeout_sec=args.ring_timeout,
        max_call_sec=args.duration,
    )
    session = SipCallSession(cfg)
    print(f"[SIP] INVITE -> {args.called}@{args.dst}:{args.dst_port}")

    rx_seen = {"n": 0}

    def on_pcmu(payload: bytes) -> None:
        rx_seen["n"] += 1
        if rx_seen["n"] == 1:
            print(f"[RTP] first packet, payload_len={len(payload)}")

    result = session.dial(args.called, on_pcmu=on_pcmu)
    if not result.ok:
        print(f"[SIP] failed: {result.error}")
        if result.status in {"403", "404", "480"}:
            print(
                "[hint] 404=被叫路由不到(号码/前缀/白名单); "
                "403=sipNumber错; 480=缺SDP或不可用. "
                "确认测试号已加白、被叫格式 a1+手机号."
            )
        raise SystemExit(2 if result.status and result.status.startswith(("4", "5")) else 1)

    print(f"[SIP] servercallid={result.servercallid or '-'}")
    print("[SIP] ACK sent, call established")
    if result.remote_rtp:
        rip, rport = result.remote_rtp
        print(
            f"[RTP] remote={rip}:{rport} local={args.bind_ip}:{args.rtp_port} "
            f"(advertised in SDP: {args.src_ip})"
        )

    session.wait_until_done(timeout=args.duration)
    if not session.is_done:
        session.hangup(reason="duration")
    else:
        # Ensure media stopped if remote BYE already finished.
        if session.rtp:
            session.rtp.stop()

    print(f"[RTP] received packets: {session.result.rx_packets}")
    print("[SIP] done")
    raise SystemExit(0)


if __name__ == "__main__":
    main()
