# -*- coding: utf-8 -*-
"""SIP TCP framing, SDP, and message builders (stdlib-only)."""
from __future__ import annotations

import re
from typing import Any


def normalize_called(phone: str, prefix: str = "a1") -> str:
    p = (phone or "").strip()
    pref = (prefix or "").strip()
    if not p:
        return p
    if pref and not p.lower().startswith(pref.lower()):
        p = pref + p.lstrip("+")
    return p


def build_sdp(src_ip: str, rtp_port: int, *, session_name: str = "AI Call") -> str:
    lines = [
        "v=0",
        f"o=- 0 0 IN IP4 {src_ip}",
        f"s={session_name}",
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
        parts = first.split(" ", 2)
        out["status"] = parts[1] if len(parts) >= 2 else None

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


def build_bye(
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
    cseq: int = 2,
) -> str:
    lines = [
        f"BYE {contact} SIP/2.0",
        f"Via: SIP/2.0/TCP {src_ip}:{src_port};branch={branch}",
    ]
    for rr in record_routes:
        lines.append(f"Route: <{rr}>")
    lines.extend(
        [
            f"From: <sip:{from_user}@{src_ip}>;tag={from_tag}",
            f"To: <{to_uri}>;tag={to_tag}",
            f"Call-ID: {call_id}",
            f"CSeq: {cseq} BYE",
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
