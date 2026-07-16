#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
最小 SIP 外呼 + RTP(PCMU) 收发 + BYE 处理。

用法（服务器上）:
  python3 scripts/sip_min_call.py --called 13824133080
  python3 scripts/sip_min_call.py --called a118324133080 --duration 30

依赖: 仅 Python 3 标准库。
"""
from __future__ import annotations

import argparse
import re
import socket
import struct
import threading
import time
from typing import Any

# --- 按你联调环境改默认值 ---
DEFAULT_DST = "124.70.154.200"
DEFAULT_DST_PORT = 5060
DEFAULT_SRC_IP = "1.13.182.189"  # 改成你服务器公网 IP
DEFAULT_SRC_SIP_PORT = 5070
DEFAULT_RTP_PORT = 10000
DEFAULT_FROM_USER = "01080435740"
DEFAULT_CALLED_PREFIX = "a1"


def normalize_called(phone: str, prefix: str = DEFAULT_CALLED_PREFIX) -> str:
    p = phone.strip()
    if not p.lower().startswith(prefix.lower()):
        p = prefix + p.lstrip("+")
    return p


def build_sdp(src_ip: str, rtp_port: int) -> str:
    lines = [
        "v=0",
        f"o=- 0 0 IN IP4 {src_ip}",
        "s=AI Call",
        f"c=IN IP4 {src_ip}",
        "t=0 0",
        f"m=audio {rtp_port} RTP/AVP 0 101",
        "a=rtpmap:0 PCMU/8000",
        "a=rtpmap:101 telephone-event/8000",
        "a=fmtp:101 0-15",
        "a=ptime:20",
        "",
    ]
    return "\r\n".join(lines)


def pop_sip_message(buf: str) -> tuple[str | None, str]:
    """Parse one complete SIP message from TCP buffer (respect Content-Length)."""
    sep = buf.find("\r\n\r\n")
    if sep < 0:
        return None, buf
    headers = buf[:sep]
    rest = buf[sep + 4 :]
    cl_m = re.search(r"(?im)^Content-Length:\s*(\d+)\s*$", headers)
    body_len = int(cl_m.group(1)) if cl_m else 0
    if len(rest) < body_len:
        return None, buf
    body = rest[:body_len]
    remainder = rest[body_len:]
    return headers + "\r\n\r\n" + body, remainder


def drain_sip_messages(buf: str) -> tuple[list[str], str]:
    messages: list[str] = []
    while True:
        msg, buf = pop_sip_message(buf)
        if msg is None:
            break
        messages.append(msg)
    return messages, buf


def parse_sdp_rtp(sdp: str) -> tuple[str, int] | None:
    ip_m = re.search(r"c=IN IP4 ([\d.]+)", sdp)
    port_m = re.search(r"m=audio (\d+)", sdp)
    if ip_m and port_m:
        return ip_m.group(1), int(port_m.group(1))
    return None


def parse_response(msg: str) -> dict[str, Any]:
    out: dict[str, Any] = {
        "status": None,
        "to_tag": None,
        "contact": None,
        "record_routes": [],
        "sdp": None,
        "servercallid": None,
    }
    first = msg.split("\r\n", 1)[0]
    if first.startswith("SIP/2.0"):
        out["status"] = first.split(" ", 2)[1] if len(first.split()) >= 2 else None

    m = re.search(r"To:.*;tag=([^\s;]+)", msg)
    if m:
        out["to_tag"] = m.group(1)

    m = re.search(r"Contact: <([^>]+)>", msg)
    if m:
        out["contact"] = m.group(1)

    for rr in re.findall(r"Record-Route: <([^>]+)>", msg):
        if rr not in out["record_routes"]:
            out["record_routes"].append(rr)

    m = re.search(r"servercallid=([^;>\s]+)", msg)
    if m:
        out["servercallid"] = m.group(1)

    if "Content-Type: application/sdp" in msg:
        _, body = msg.split("\r\n\r\n", 1)
        out["sdp"] = body.strip()

    return out


def build_invite(
    *,
    dst_ip: str,
    dst_port: int,
    src_ip: str,
    src_port: int,
    called: str,
    from_user: str,
    branch: str,
    from_tag: str,
    call_id: str,
    sdp: str,
) -> str:
    return "\r\n".join(
        [
            f"INVITE sip:{called}@{dst_ip}:{dst_port};transport=tcp SIP/2.0",
            f"Via: SIP/2.0/TCP {src_ip}:{src_port};branch={branch}",
            "Max-Forwards: 70",
            f"From: <sip:{from_user}@{src_ip}>;tag={from_tag}",
            f"To: <sip:{called}@{dst_ip}>",
            f"Call-ID: {call_id}",
            "CSeq: 1 INVITE",
            f"Contact: <sip:{from_user}@{src_ip}:{src_port};transport=tcp>",
            "Allow: INVITE, ACK, CANCEL, BYE, OPTIONS",
            "Content-Type: application/sdp",
            f"Content-Length: {len(sdp.encode('utf-8'))}",
            "",
            sdp,
        ]
    )


def build_ack(
    *,
    contact: str,
    src_ip: str,
    src_port: int,
    from_user: str,
    from_tag: str,
    to_uri: str,
    to_tag: str,
    call_id: str,
    record_routes: list[str],
    branch: str,
) -> str:
    lines = [
        f"ACK {contact} SIP/2.0",
        f"Via: SIP/2.0/TCP {src_ip}:{src_port};branch={branch}",
    ]
    for rr in record_routes:
        lines.append(f"Route: <{rr}>")
    lines.extend(
        [
            f"From: <sip:{from_user}@{src_ip}>;tag={from_tag}",
            f"To: <{to_uri}>;tag={to_tag}",
            f"Call-ID: {call_id}",
            "CSeq: 1 ACK",
            "Content-Length: 0",
            "",
        ]
    )
    return "\r\n".join(lines)


def build_bye_200(via_lines: list[str], from_h: str, to_h: str, call_id: str, cseq: str) -> str:
    lines = ["SIP/2.0 200 OK"]
    lines.extend(via_lines)
    lines.extend(
        [
            from_h,
            to_h,
            f"Call-ID: {call_id}",
            f"CSeq: {cseq}",
            "Content-Length: 0",
            "",
        ]
    )
    return "\r\n".join(lines)


# PCMU silence payload (20ms @ 8kHz = 160 bytes of 0xFF)
PCMU_SILENCE = b"\xff" * 160


def rtp_packet(seq: int, ts: int, ssrc: int, payload: bytes) -> bytes:
    # V=2, P=0, X=0, CC=0, M=0, PT=0 (PCMU)
    header = struct.pack("!BBHII", 0x80, 0, seq & 0xFFFF, ts & 0xFFFFFFFF, ssrc & 0xFFFFFFFF)
    return header + payload


class RtpSession:
    def __init__(self, local_port: int, remote_ip: str, remote_port: int, src_ip: str) -> None:
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind((src_ip, local_port))
        self.sock.settimeout(0.5)
        self.remote = (remote_ip, remote_port)
        self.seq = 0
        self.ts = 0
        self.ssrc = 0xA1C411
        self.rx_count = 0
        self._stop = threading.Event()

    def send_silence_loop(self) -> None:
        """Keepalive: send 20ms PCMU silence so对端不判死。"""
        while not self._stop.is_set():
            pkt = rtp_packet(self.seq, self.ts, self.ssrc, PCMU_SILENCE)
            try:
                self.sock.sendto(pkt, self.remote)
            except OSError:
                pass
            self.seq = (self.seq + 1) & 0xFFFF
            self.ts = (self.ts + 160) & 0xFFFFFFFF
            time.sleep(0.02)

    def recv_loop(self) -> None:
        while not self._stop.is_set():
            try:
                data, addr = self.sock.recvfrom(2048)
                if len(data) >= 12:
                    self.rx_count += 1
                    if self.rx_count == 1:
                        print(f"[RTP] first packet from {addr}, len={len(data)}")
            except socket.timeout:
                continue
            except OSError:
                break

    def stop(self) -> None:
        self._stop.set()
        try:
            self.sock.close()
        except OSError:
            pass


def run_call(args: argparse.Namespace) -> int:
    called = normalize_called(args.called, args.prefix)
    src_ip = args.src_ip
    sdp = build_sdp(src_ip, args.rtp_port)
    branch = f"z9hG4bK{int(time.time() * 1000)}"
    from_tag = f"tag{int(time.time())}"
    call_id = f"call-{int(time.time())}@{src_ip}"

    invite = build_invite(
        dst_ip=args.dst,
        dst_port=args.dst_port,
        src_ip=src_ip,
        src_port=args.src_port,
        called=called,
        from_user=args.from_user,
        branch=branch,
        from_tag=from_tag,
        call_id=call_id,
        sdp=sdp,
    )

    print(f"[SIP] INVITE -> {called}@{args.dst}:{args.dst_port}")
    tcp = socket.create_connection((args.dst, args.dst_port), timeout=10)
    tcp.settimeout(args.ring_timeout)
    tcp.sendall(invite.encode("utf-8"))

    buf = ""
    to_uri = f"sip:{called}@{args.dst}"
    contact = None
    to_tag = None
    record_routes: list[str] = []
    remote_rtp: tuple[str, int] | None = None
    answered = False

    def handle_message(msg: str) -> str | None:
        """Process one SIP message. Return 'answered', 'failed', 'bye', or None."""
        nonlocal contact, to_tag, record_routes, remote_rtp, answered

        if msg.startswith("BYE "):
            print("[SIP] received BYE, sending 200 OK")
            via = re.findall(r"^(Via: .+)$", msg, re.M)
            from_h = re.search(r"^(From: .+)$", msg, re.M)
            to_h = re.search(r"^(To: .+)$", msg, re.M)
            cseq_m = re.search(r"^(CSeq: .+)$", msg, re.M)
            if from_h and to_h and cseq_m:
                resp = build_bye_200(
                    via,
                    to_h.group(1),
                    from_h.group(1),
                    call_id,
                    cseq_m.group(1),
                )
                tcp.sendall(resp.encode("utf-8"))
            return "bye"

        if not msg.startswith("SIP/2.0"):
            return None

        status_line = msg.split("\r\n", 1)[0]
        print(status_line)

        p = parse_response(msg)
        if p["to_tag"]:
            to_tag = p["to_tag"]
        if p["contact"]:
            contact = p["contact"]
        if p["record_routes"]:
            record_routes = p["record_routes"]
        if p["sdp"]:
            rtp = parse_sdp_rtp(p["sdp"])
            if rtp:
                remote_rtp = rtp
        if p["servercallid"]:
            print(f"[SIP] servercallid={p['servercallid']}")

        st = p["status"]
        if st and st.startswith(("4", "5")) and st != "491":
            reason = re.search(r"^Reason: (.+)$", msg, re.M)
            extra = f" ({reason.group(1)})" if reason else ""
            print(f"[SIP] call failed: {st}{extra}")
            print(
                "[hint] 404=被叫路由不到(号码/前缀/白名单); "
                "403=sipNumber错; 480=缺SDP或不可用. "
                "确认测试号已加白、被叫格式 a1+手机号."
            )
            return "failed"

        if st == "200":
            answered = True
            return "answered"
        return None

    deadline = time.time() + args.ring_timeout
    while time.time() < deadline:
        try:
            chunk = tcp.recv(65535)
            if not chunk:
                break
            buf += chunk.decode("utf-8", "replace")
        except socket.timeout:
            if answered:
                break
            continue

        messages, buf = drain_sip_messages(buf)
        for msg in messages:
            result = handle_message(msg)
            if result == "failed":
                tcp.close()
                return 2
            if result == "answered":
                break
            if result == "bye":
                return 0
        if answered:
            break

    if not answered or not contact or not to_tag:
        print("[SIP] no 200 OK / missing Contact or To tag")
        tcp.close()
        return 1

    ack_branch = f"z9hG4bK{int(time.time() * 1000)}"
    ack = build_ack(
        contact=contact,
        src_ip=src_ip,
        src_port=args.src_port,
        from_user=args.from_user,
        from_tag=from_tag,
        to_uri=to_uri,
        to_tag=to_tag,
        call_id=call_id,
        record_routes=record_routes,
        branch=ack_branch,
    )
    tcp.sendall(ack.encode("utf-8"))
    print("[SIP] ACK sent, call established")

    if not remote_rtp:
        print("[RTP] no remote SDP, exiting")
        tcp.close()
        return 1

    rip, rport = remote_rtp
    print(f"[RTP] remote={rip}:{rport} local={src_ip}:{args.rtp_port}")

    # 安全组需放行 UDP rtp_port 入站
    rtp = RtpSession(args.rtp_port, rip, rport, src_ip)
    t_tx = threading.Thread(target=rtp.send_silence_loop, daemon=True)
    t_rx = threading.Thread(target=rtp.recv_loop, daemon=True)
    t_tx.start()
    t_rx.start()

    tcp.settimeout(1.0)
    end = time.time() + args.duration
    try:
        while time.time() < end:
            try:
                chunk = tcp.recv(65535)
                if not chunk:
                    break
                buf += chunk.decode("utf-8", "replace")
                messages, buf = drain_sip_messages(buf)
                for msg in messages:
                    if msg.startswith("BYE "):
                        handle_message(msg)
                        end = 0
                        break
            except socket.timeout:
                continue
    finally:
        rtp.stop()
        tcp.close()
        print(f"[RTP] received packets: {rtp.rx_count}")
        print("[SIP] done")

    return 0


def main() -> None:
    ap = argparse.ArgumentParser(description="Minimal SIP INVITE/ACK + RTP + BYE")
    ap.add_argument("--called", required=True, help="被叫手机号，可带或不带 a1 前缀")
    ap.add_argument("--prefix", default=DEFAULT_CALLED_PREFIX)
    ap.add_argument("--dst", default=DEFAULT_DST)
    ap.add_argument("--dst-port", type=int, default=DEFAULT_DST_PORT)
    ap.add_argument("--src-ip", default=DEFAULT_SRC_IP)
    ap.add_argument("--src-port", type=int, default=DEFAULT_SRC_SIP_PORT)
    ap.add_argument("--rtp-port", type=int, default=DEFAULT_RTP_PORT)
    ap.add_argument("--from-user", default=DEFAULT_FROM_USER)
    ap.add_argument("--ring-timeout", type=float, default=45.0, help="等 200 OK 秒数")
    ap.add_argument("--duration", type=float, default=20.0, help="接通后保持秒数")
    args = ap.parse_args()
    raise SystemExit(run_call(args))


if __name__ == "__main__":
    main()
