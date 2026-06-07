"""Streaming PCM16 denoising session for Omni uplink."""

from __future__ import annotations

import numpy as np

from backend.hush.weya_nc import WeyaNC


class HushDenoiseSession:
    """
    Buffer arbitrary-size int16 PCM chunks and emit denoised int16 PCM frames.

    Follows the frame buffering pattern from Hush deployment/examples/python/denoise_stream.py.
    """

    def __init__(self, nc: WeyaNC) -> None:
        self._nc = nc
        self._frame_len = nc.frame_length
        self._bytes_per_frame = self._frame_len * 2
        self._buf = bytearray()

    @property
    def frame_length(self) -> int:
        return self._frame_len

    def process_pcm_s16le(self, pcm_chunk: bytes) -> bytes:
        """Append uplink PCM and return all complete denoised frames."""
        if not pcm_chunk:
            return b""
        self._buf.extend(pcm_chunk)
        out = bytearray()
        while len(self._buf) >= self._bytes_per_frame:
            raw = bytes(self._buf[: self._bytes_per_frame])
            del self._buf[: self._bytes_per_frame]
            frame_in = np.frombuffer(raw, dtype=np.int16)
            frame_out = self._nc.process_frame(frame_in)
            out.extend(frame_out.tobytes())
        return bytes(out)

    def reset(self) -> None:
        self._buf.clear()
        self._nc.reset()

    def close(self) -> None:
        self._buf.clear()
        self._nc.close()
