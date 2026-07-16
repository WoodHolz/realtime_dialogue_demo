# -*- coding: utf-8 -*-
"""SIP UAC call session: INVITE → ACK → RTP → BYE (scenario-agnostic)."""
from __future__ import annotations

import logging
import re
import socket
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from backend.sip.config import SipConfig
from backend.sip.rtp import RtpSession
from backend.sip.signaling import (
    build_ack,
    build_bye,
    build_bye_200,
    build_invite,
    build_sdp,
    drain_sip_messages,
    normalize_called,
    parse_response,
    parse_sdp_rtp,
)

LOG = logging.getLogger(__name__)


@dataclass
class SipCallResult:
    ok: bool
    called: str = ""
    sip_call_id: str = ""
    servercallid: str = ""
    remote_rtp: tuple[str, int] | None = None
    status: str = ""
    error: str = ""
    rx_packets: int = 0
    tx_packets: int = 0
    hangup_reason: str = ""
    meta: dict[str, Any] = field(default_factory=dict)


class SipCallSession:
    """
    Outbound SIP call with optional media callbacks.

    Typical flow:
      session = SipCallSession(cfg)
      result = session.dial(phone, on_answered=...)
      # RTP running; push TTS via session.enqueue_pcmu / session.rtp
      session.wait_until_done(timeout=...)
      session.hangup()  # or remote BYE ends the call
    """

    def __init__(self, config: SipConfig) -> None:
        self.cfg = config
        self.rtp: RtpSession | None = None
        self._tcp: socket.socket | None = None
        self._buf = ""
        self._lock = threading.Lock()
        self._done = threading.Event()
        self._answered = threading.Event()
        self._result = SipCallResult(ok=False)
        self._contact: str | None = None
        self._to_tag: str | None = None
        self._to_uri = ""
        self._from_tag = ""
        self._sip_call_id = ""
        self._record_routes: list[str] = []
        self._called = ""
        self._cseq = 1
        self._sip_reader: threading.Thread | None = None
        self._on_pcmu: Callable[[bytes], None] | None = None

    @property
    def result(self) -> SipCallResult:
        return self._result

    @property
    def is_done(self) -> bool:
        return self._done.is_set()

    def enqueue_pcmu(self, payload: bytes) -> None:
        if self.rtp:
            self.rtp.enqueue_pcmu(payload)

    def dial(
        self,
        phone: str,
        *,
        on_pcmu: Callable[[bytes], None] | None = None,
        on_answered: Callable[["SipCallSession"], None] | None = None,
        rtp_port: int | None = None,
    ) -> SipCallResult:
        """Blocking dial until answered or failed. Starts RTP on success."""
        cfg = self.cfg
        self._on_pcmu = on_pcmu
        called = normalize_called(phone, cfg.called_prefix)
        self._called = called
        rtp_port = int(rtp_port or cfg.rtp_port)
        sdp = build_sdp(cfg.src_ip, rtp_port)
        branch = f"z9hG4bK{int(time.time() * 1000)}"
        self._from_tag = f"tag{int(time.time())}"
        self._sip_call_id = f"call-{int(time.time())}@{cfg.src_ip}"
        self._to_uri = f"sip:{called}@{cfg.dst_host}"

        invite = build_invite(
            dst_ip=cfg.dst_host,
            dst_port=cfg.dst_port,
            src_ip=cfg.src_ip,
            src_port=cfg.src_sip_port,
            called=called,
            from_user=cfg.from_user,
            branch=branch,
            from_tag=self._from_tag,
            call_id=self._sip_call_id,
            sdp=sdp,
        )

        LOG.info("SIP INVITE -> %s@%s:%s", called, cfg.dst_host, cfg.dst_port)
        try:
            tcp = socket.create_connection((cfg.dst_host, cfg.dst_port), timeout=10)
        except OSError as exc:
            self._result = SipCallResult(ok=False, called=called, error=f"connect failed: {exc}")
            self._done.set()
            return self._result

        self._tcp = tcp
        tcp.settimeout(cfg.ring_timeout_sec)
        tcp.sendall(invite.encode("utf-8"))

        remote_rtp: tuple[str, int] | None = None
        failed = False
        deadline = time.time() + cfg.ring_timeout_sec

        while time.time() < deadline and not self._answered.is_set():
            try:
                chunk = tcp.recv(65535)
                if not chunk:
                    break
                self._buf += chunk.decode("utf-8", "replace")
            except socket.timeout:
                break

            messages, self._buf = drain_sip_messages(self._buf)
            for msg in messages:
                outcome, remote_rtp = self._handle_pre_answer(msg, remote_rtp)
                if outcome == "failed":
                    failed = True
                    break
                if outcome == "bye":
                    failed = True
                    break
            if failed:
                break

        if failed or not self._answered.is_set() or not self._contact or not self._to_tag:
            err = self._result.error or "no 200 OK / missing Contact or To tag"
            self._result = SipCallResult(
                ok=False,
                called=called,
                sip_call_id=self._sip_call_id,
                status=self._result.status,
                error=err,
                servercallid=self._result.servercallid,
            )
            self._close_tcp()
            self._done.set()
            return self._result

        ack_branch = f"z9hG4bK{int(time.time() * 1000)}"
        ack = build_ack(
            contact=self._contact,
            src_ip=cfg.src_ip,
            src_port=cfg.src_sip_port,
            from_user=cfg.from_user,
            from_tag=self._from_tag,
            to_uri=self._to_uri,
            to_tag=self._to_tag,
            call_id=self._sip_call_id,
            record_routes=self._record_routes,
            branch=ack_branch,
        )
        tcp.sendall(ack.encode("utf-8"))
        LOG.info("SIP ACK sent, call established call_id=%s", self._sip_call_id)

        if not remote_rtp:
            self._result = SipCallResult(
                ok=False,
                called=called,
                sip_call_id=self._sip_call_id,
                error="no remote SDP",
                servercallid=self._result.servercallid,
            )
            self._close_tcp()
            self._done.set()
            return self._result

        rip, rport = remote_rtp
        LOG.info(
            "RTP remote=%s:%s local=%s:%s (SDP src_ip=%s)",
            rip,
            rport,
            cfg.bind_ip,
            rtp_port,
            cfg.src_ip,
        )
        self.rtp = RtpSession(
            rtp_port,
            rip,
            rport,
            bind_ip=cfg.bind_ip,
            on_payload=self._on_pcmu,
        )
        self.rtp.start()
        self._result = SipCallResult(
            ok=True,
            called=called,
            sip_call_id=self._sip_call_id,
            servercallid=self._result.servercallid,
            remote_rtp=remote_rtp,
            status="200",
            meta={
                "src_ip": cfg.src_ip,
                "bind_ip": cfg.bind_ip,
                "rtp_port": rtp_port,
                "from_user": cfg.from_user,
            },
        )
        self._sip_reader = threading.Thread(target=self._read_sip_loop, name="sip-read", daemon=True)
        self._sip_reader.start()
        if on_answered:
            try:
                on_answered(self)
            except Exception:
                LOG.exception("on_answered callback failed")
        return self._result

    def wait_until_done(self, timeout: float | None = None) -> SipCallResult:
        self._done.wait(timeout=timeout)
        self._finalize_counts()
        return self._result

    def hangup(self, reason: str = "local") -> None:
        """Send BYE if still connected and stop media."""
        with self._lock:
            if self._done.is_set():
                return
            tcp = self._tcp
            contact = self._contact
            to_tag = self._to_tag
        if tcp and contact and to_tag:
            try:
                self._cseq += 1
                branch = f"z9hG4bK{int(time.time() * 1000)}"
                bye = build_bye(
                    contact=contact,
                    src_ip=self.cfg.src_ip,
                    src_port=self.cfg.src_sip_port,
                    from_user=self.cfg.from_user,
                    from_tag=self._from_tag,
                    to_uri=self._to_uri,
                    to_tag=to_tag,
                    call_id=self._sip_call_id,
                    record_routes=self._record_routes,
                    branch=branch,
                    cseq=self._cseq,
                )
                tcp.sendall(bye.encode("utf-8"))
                LOG.info("SIP BYE sent reason=%s", reason)
            except OSError:
                LOG.debug("BYE send failed", exc_info=True)
        self._finish(hangup_reason=reason)

    def _handle_pre_answer(
        self,
        msg: str,
        remote_rtp: tuple[str, int] | None,
    ) -> tuple[str | None, tuple[str, int] | None]:
        if msg.startswith("BYE "):
            self._answer_bye(msg)
            self._result.error = "remote BYE before answer"
            return "bye", remote_rtp

        if not msg.startswith("SIP/2.0"):
            return None, remote_rtp

        status_line = msg.split("\r\n", 1)[0]
        LOG.info("%s", status_line)
        p = parse_response(msg)
        if p["to_tag"]:
            self._to_tag = p["to_tag"]
        if p["contact"]:
            self._contact = p["contact"]
        if p["record_routes"]:
            self._record_routes = list(p["record_routes"])
        if p["sdp"]:
            rtp = parse_sdp_rtp(p["sdp"])
            if rtp:
                remote_rtp = rtp
        if p["servercallid"]:
            self._result.servercallid = p["servercallid"]
            LOG.info("SIP servercallid=%s", p["servercallid"])

        st = p["status"] or ""
        self._result.status = st
        if st.startswith(("4", "5")) and st != "491":
            reason = re.search(r"^Reason: (.+)$", msg, re.M)
            extra = f" ({reason.group(1)})" if reason else ""
            self._result.error = f"call failed: {st}{extra}"
            LOG.warning("SIP %s", self._result.error)
            return "failed", remote_rtp

        if st == "200":
            self._answered.set()
            return "answered", remote_rtp
        return None, remote_rtp

    def _read_sip_loop(self) -> None:
        tcp = self._tcp
        if not tcp:
            return
        tcp.settimeout(1.0)
        while not self._done.is_set():
            try:
                chunk = tcp.recv(65535)
                if not chunk:
                    self._finish(hangup_reason="tcp_closed")
                    break
                self._buf += chunk.decode("utf-8", "replace")
                messages, self._buf = drain_sip_messages(self._buf)
                for msg in messages:
                    if msg.startswith("BYE "):
                        self._answer_bye(msg)
                        self._finish(hangup_reason="remote_bye")
                        return
            except socket.timeout:
                continue
            except OSError:
                self._finish(hangup_reason="tcp_error")
                break

    def _answer_bye(self, msg: str) -> None:
        tcp = self._tcp
        if not tcp:
            return
        via = re.findall(r"^(Via: .+)$", msg, re.M)
        from_h = re.search(r"^(From: .+)$", msg, re.M)
        to_h = re.search(r"^(To: .+)$", msg, re.M)
        cseq_m = re.search(r"^(CSeq: .+)$", msg, re.M)
        if from_h and to_h and cseq_m:
            # Swap From/To for response (same as sip_min_call).
            resp = build_bye_200(
                via,
                to_h.group(1),
                from_h.group(1),
                self._sip_call_id,
                cseq_m.group(1),
            )
            try:
                tcp.sendall(resp.encode("utf-8"))
            except OSError:
                pass
            LOG.info("SIP answered remote BYE with 200 OK")

    def _finish(self, hangup_reason: str = "") -> None:
        with self._lock:
            if self._done.is_set():
                return
            self._done.set()
        if hangup_reason:
            self._result.hangup_reason = hangup_reason
        if self.rtp:
            self.rtp.stop()
        self._finalize_counts()
        self._close_tcp()

    def _finalize_counts(self) -> None:
        if self.rtp:
            self._result.rx_packets = self.rtp.rx_count
            self._result.tx_packets = self.rtp.tx_count

    def _close_tcp(self) -> None:
        tcp = self._tcp
        self._tcp = None
        if tcp:
            try:
                tcp.close()
            except OSError:
                pass
