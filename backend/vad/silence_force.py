from __future__ import annotations


class SilenceForceTracker:
    """Accumulate sustained silence during an active turn; force end when threshold is met."""

    def __init__(self, force_secs: float) -> None:
        self._force_ms = max(0.0, force_secs) * 1000.0
        self._elapsed_ms = 0.0

    @property
    def enabled(self) -> bool:
        return self._force_ms > 0

    def reset(self) -> None:
        self._elapsed_ms = 0.0

    def tick(self, frame_ms: float) -> None:
        if not self.enabled:
            return
        self._elapsed_ms += max(0.0, frame_ms)

    def reached(self) -> bool:
        return self.enabled and self._elapsed_ms >= self._force_ms

    @property
    def elapsed_ms(self) -> float:
        return self._elapsed_ms

    def force_meta(self, *, reason: str = "silence_timeout") -> dict[str, object]:
        return {
            "forced": True,
            "force_reason": reason,
            "silence_ms": round(self._elapsed_ms),
        }
