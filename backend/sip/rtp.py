# -*- coding: utf-8 -*-
"""RTP session for PCMU 8 kHz bidirectional media."""
from __future__ import annotations

import logging
import queue
import socket
import struct
import threading
import time
from collections.abc import Callable

from backend.sip.codec import PCMU_FRAME_BYTES, PCMU_SILENCE

LOG = logging.getLogger(__name__)


def rtp_packet(seq: int, ts: int, ssrc: int, payload: bytes, *, marker: bool = False) -> bytes:
    # V=2, P=0, X=0, CC=0, PT=0 (PCMU)
    pt = 0 | (0x80 if marker else 0)
    header = struct.pack("!BBHII", 0x80, pt, seq & 0xFFFF, ts & 0xFFFFFFFF, ssrc & 0xFFFFFFFF)
    return header + payload


class RtpSession:
    """
    Bidirectional PCMU RTP.

    TX: prefer queued payloads (AI TTS); otherwise silence keepalive.
    RX: invoke on_payload(pcmu_bytes) for each complete RTP payload.
    """

    def __init__(
        self,
        local_port: int,
        remote_ip: str,
        remote_port: int,
        *,
        bind_ip: str = "0.0.0.0",
        on_payload: Callable[[bytes], None] | None = None,
        ssrc: int = 0xA1C411,
    ) -> None:
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind((bind_ip, local_port))
        self.sock.settimeout(0.5)
        self.remote = (remote_ip, remote_port)
        self.seq = 0
        self.ts = 0
        self.ssrc = ssrc
        self.rx_count = 0
        self.tx_count = 0
        self.on_payload = on_payload
        self._tx_q: queue.Queue[bytes | None] = queue.Queue(maxsize=500)
        self._stop = threading.Event()
        self._tx_thread: threading.Thread | None = None
        self._rx_thread: threading.Thread | None = None

    def start(self) -> None:
        self._tx_thread = threading.Thread(target=self._send_loop, name="rtp-tx", daemon=True)
        self._rx_thread = threading.Thread(target=self._recv_loop, name="rtp-rx", daemon=True)
        self._tx_thread.start()
        self._rx_thread.start()

    def enqueue_pcmu(self, payload: bytes) -> None:
        """Queue one or more PCMU frames (multiples of 160 preferred)."""
        if not payload or self._stop.is_set():
            return
        # Split into 20ms frames so ptime stays aligned.
        offset = 0
        while offset + PCMU_FRAME_BYTES <= len(payload):
            frame = payload[offset : offset + PCMU_FRAME_BYTES]
            offset += PCMU_FRAME_BYTES
            try:
                self._tx_q.put_nowait(frame)
            except queue.Full:
                try:
                    self._tx_q.get_nowait()
                except queue.Empty:
                    pass
                try:
                    self._tx_q.put_nowait(frame)
                except queue.Full:
                    break
        # Drop trailing partial frame (caller should use PcmuFrameBuffer).

    def _send_loop(self) -> None:
        next_t = time.monotonic()
        while not self._stop.is_set():
            payload = PCMU_SILENCE
            try:
                item = self._tx_q.get_nowait()
                if item is None:
                    break
                if len(item) == PCMU_FRAME_BYTES:
                    payload = item
                elif len(item) > PCMU_FRAME_BYTES:
                    payload = item[:PCMU_FRAME_BYTES]
            except queue.Empty:
                pass
            pkt = rtp_packet(self.seq, self.ts, self.ssrc, payload)
            try:
                self.sock.sendto(pkt, self.remote)
                self.tx_count += 1
            except OSError:
                pass
            self.seq = (self.seq + 1) & 0xFFFF
            self.ts = (self.ts + PCMU_FRAME_BYTES) & 0xFFFFFFFF
            next_t += 0.02
            delay = next_t - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            else:
                next_t = time.monotonic()

    def _recv_loop(self) -> None:
        while not self._stop.is_set():
            try:
                data, addr = self.sock.recvfrom(2048)
            except socket.timeout:
                continue
            except OSError:
                break
            if len(data) < 12:
                continue
            self.rx_count += 1
            if self.rx_count == 1:
                LOG.info("RTP first packet from %s len=%s", addr, len(data))
            # Skip RTP header (12 + CSRC*4); ignore extensions for now.
            cc = data[0] & 0x0F
            header_len = 12 + cc * 4
            if data[0] & 0x10:  # extension
                if len(data) < header_len + 4:
                    continue
                ext_len = int.from_bytes(data[header_len + 2 : header_len + 4], "big")
                header_len += 4 + ext_len * 4
            payload = data[header_len:]
            if not payload:
                continue
            if self.on_payload:
                try:
                    self.on_payload(payload)
                except Exception:
                    LOG.exception("RTP on_payload failed")

    def stop(self) -> None:
        self._stop.set()
        try:
            self._tx_q.put_nowait(None)
        except queue.Full:
            pass
        try:
            self.sock.close()
        except OSError:
            pass
        for t in (self._tx_thread, self._rx_thread):
            if t and t.is_alive():
                t.join(timeout=1.0)
