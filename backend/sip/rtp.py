# -*- coding: utf-8 -*-
"""RTP session for PCMU 8 kHz bidirectional media."""
from __future__ import annotations

import logging
import os
import queue
import socket
import struct
import threading
import time
from collections.abc import Callable

from backend.sip.codec import PCMU_FRAME_BYTES, PCMU_SILENCE

LOG = logging.getLogger(__name__)

# ~20ms PCMU frames. Hard cap ≈ 60s; soft pace keeps ~1s ahead of realtime sender.
_DEFAULT_TX_QUEUE_FRAMES = 3000
_DEFAULT_TX_PACE_HIGH_FRAMES = 50


def _env_int(name: str, default: int) -> int:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        return max(1, int(raw))
    except ValueError:
        return default


def rtp_packet(seq: int, ts: int, ssrc: int, payload: bytes, *, marker: bool = False) -> bytes:
    # V=2, P=0, X=0, CC=0, PT=0 (PCMU)
    pt = 0 | (0x80 if marker else 0)
    header = struct.pack("!BBHII", 0x80, pt, seq & 0xFFFF, ts & 0xFFFFFFFF, ssrc & 0xFFFFFFFF)
    return header + payload


def allocate_rtp_socket(
    bind_ip: str,
    preferred_port: int,
    *,
    port_count: int = 50,
) -> tuple[socket.socket, int]:
    """
    Bind a UDP socket before SDP/INVITE so the advertised port is actually ours.

    Tries preferred_port .. preferred_port+port_count-1 (even ports preferred for RTP).
    """
    if preferred_port <= 0:
        preferred_port = 10000
    if port_count < 1:
        port_count = 1
    last_err: OSError | None = None
    for offset in range(port_count):
        port = preferred_port + offset
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            # Do not set SO_REUSEADDR: on Linux UDP it can "succeed" while another
            # process holds the port, and RTP packets would be split/lost.
            sock.bind((bind_ip, port))
            sock.settimeout(0.5)
            if offset:
                LOG.info("RTP preferred port busy; bound %s:%s instead", bind_ip, port)
            return sock, port
        except OSError as exc:
            last_err = exc
            try:
                sock.close()
            except OSError:
                pass
    raise OSError(
        f"cannot bind RTP on {bind_ip}:{preferred_port}+{port_count - 1}: {last_err}"
    ) from last_err


class RtpSession:
    """
    Bidirectional PCMU RTP.

    TX: prefer queued payloads (AI TTS); otherwise silence keepalive.
    RX: invoke on_payload(pcmu_bytes) for each complete RTP payload.

    Soft 1x pacing: enqueue blocks when TX is already ~pace_high ahead of the
    20ms sender, instead of bursting then dropping oldest on Full (that caused
    mid-utterance garble on phones while recordings still looked fine).
    """

    def __init__(
        self,
        local_port: int,
        remote_ip: str,
        remote_port: int,
        *,
        bind_ip: str = "0.0.0.0",
        sock: socket.socket | None = None,
        on_payload: Callable[[bytes], None] | None = None,
        ssrc: int = 0xA1C411,
    ) -> None:
        if sock is not None:
            self.sock = sock
            self.local_port = int(self.sock.getsockname()[1])
        else:
            self.sock, self.local_port = allocate_rtp_socket(bind_ip, local_port, port_count=1)
        self.remote = (remote_ip, remote_port)
        self.seq = 0
        self.ts = 0
        self.ssrc = ssrc
        self.rx_count = 0
        self.tx_count = 0
        self.on_payload = on_payload
        self._tx_queue_max = _env_int("SIP_RTP_TX_QUEUE_FRAMES", _DEFAULT_TX_QUEUE_FRAMES)
        self._pace_high = _env_int("SIP_RTP_TX_PACE_HIGH_FRAMES", _DEFAULT_TX_PACE_HIGH_FRAMES)
        if self._pace_high >= self._tx_queue_max:
            self._pace_high = max(1, self._tx_queue_max // 2)
        self._tx_q: queue.Queue[bytes | None] = queue.Queue(maxsize=self._tx_queue_max)
        self._stop = threading.Event()
        self._tx_thread: threading.Thread | None = None
        self._rx_thread: threading.Thread | None = None

    def start(self) -> None:
        self._tx_thread = threading.Thread(target=self._send_loop, name="rtp-tx", daemon=True)
        self._rx_thread = threading.Thread(target=self._recv_loop, name="rtp-rx", daemon=True)
        self._tx_thread.start()
        self._rx_thread.start()
        LOG.info(
            "RTP TX queue max_frames=%s pace_high=%s (~%.1fs soft lead)",
            self._tx_queue_max,
            self._pace_high,
            self._pace_high * 0.02,
        )

    def _put_tx_frame(self, frame: bytes) -> bool:
        """Enqueue one 20ms PCMU frame with soft 1x pacing; never drop on Full."""
        if not frame or self._stop.is_set():
            return False
        # Soft realtime: wait while already ~pace_high ahead of the sender.
        while not self._stop.is_set() and self._tx_q.qsize() >= self._pace_high:
            time.sleep(0.01)
        while not self._stop.is_set():
            try:
                self._tx_q.put(frame, timeout=0.2)
                return True
            except queue.Full:
                # Hard cap: wait for send_loop — never drop oldest TTS frames.
                continue
        return False

    def enqueue_pcmu(self, payload: bytes) -> None:
        """Queue one or more PCMU frames (multiples of 160 preferred).

        May block briefly for soft 1x pacing. Never drops frames when full.
        """
        if not payload or self._stop.is_set():
            return
        offset = 0
        while offset + PCMU_FRAME_BYTES <= len(payload):
            frame = payload[offset : offset + PCMU_FRAME_BYTES]
            offset += PCMU_FRAME_BYTES
            if not self._put_tx_frame(frame):
                return

    def clear_tx_queue(self) -> int:
        """Drop queued TTS frames (barge-in / new utterance). Returns dropped count."""
        dropped = 0
        while True:
            try:
                item = self._tx_q.get_nowait()
            except queue.Empty:
                break
            if item is None:
                try:
                    self._tx_q.put_nowait(None)
                except queue.Full:
                    pass
                break
            dropped += 1
        return dropped

    def tx_queue_size(self) -> int:
        """Approximate number of PCMU frames still waiting to be sent."""
        return self._tx_q.qsize()

    def enqueue_silence_frames(self, n: int) -> None:
        """Enqueue n × 20ms PCMU silence frames (breath gap / pad)."""
        if n <= 0 or self._stop.is_set():
            return
        for _ in range(n):
            if not self._put_tx_frame(PCMU_SILENCE):
                break

    def wait_tx_idle(
        self,
        *,
        timeout_sec: float = 30.0,
        quiet_frames: int = 2,
        poll_sec: float = 0.02,
    ) -> bool:
        """Block until TX queue is empty and quiet_frames silence ticks have passed.

        TTS generation end ≠ phone playback end: PCMU may still sit in _tx_q.
        After the queue drains, wait a few send-loop ticks so the last dequeued
        frame has also been put on the wire.

        Returns True if idle reached, False on timeout or stop.
        """
        deadline = time.monotonic() + max(0.0, timeout_sec)
        quiet_need = max(0, int(quiet_frames))
        quiet_got = 0
        while not self._stop.is_set():
            if self._tx_q.qsize() > 0:
                quiet_got = 0
            else:
                quiet_got += 1
                if quiet_got >= quiet_need:
                    return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(poll_sec)
        return False

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
