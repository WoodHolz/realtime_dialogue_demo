from __future__ import annotations

import logging
from typing import Any

from backend.duplug_vad_client import duplug_server_url, _load_duplug_vad_config
from backend.duplug_vad_local import duplug_local_config_path, duplug_local_enabled
from backend.vad.config import (
    normalize_vad_provider,
    resolve_vad_provider,
    vad_provider,
)
from backend.vad.duplug_backend import DuplugBackend, get_duplug_local_manager, init_duplug_local_manager

LOG = logging.getLogger(__name__)


def create_vad_backend(
    provider: str | None = None,
    *,
    force_embedded_asr: bool | None = None,
    livekit_stt_backend: str | None = None,
    livekit_turn_threshold: float | str | None = None,
):
    active = normalize_vad_provider(provider) or resolve_vad_provider(override=provider)
    LOG.info("Creating VAD backend: provider=%s (requested=%r)", active, provider)
    if active == "smart_turn_v3":
        try:
            from backend.vad.smart_turn_v3 import SmartTurnV3Backend

            return SmartTurnV3Backend()
        except Exception as exc:  # noqa: BLE001
            LOG.warning("Smart Turn v3 init failed: %s", exc)
            return None
    if active == "livekit_turn_detector":
        try:
            from backend.vad.livekit_turn_detector import LivekitTurnDetectorBackend

            return LivekitTurnDetectorBackend(
                force_embedded_asr=force_embedded_asr,
                stt_backend=livekit_stt_backend,
                turn_threshold=livekit_turn_threshold,
            )
        except Exception as exc:  # noqa: BLE001
            LOG.warning("LiveKit turn-detector init failed: %s", exc)
            return None
    if active == "duplug":
        duplug_on, _ = _load_duplug_vad_config()
        if not duplug_on:
            LOG.warning("Duplug VAD requested but not enabled")
            return None
        local = get_duplug_local_manager()
        if local is None and duplug_local_enabled():
            local = init_duplug_local_manager(duplug_local_config_path())
        return DuplugBackend(local_manager=local, remote_url=duplug_server_url())
    LOG.warning("Unknown or disabled VAD provider: %s", active)
    return None


def vad_status_on_init_failure(provider: str, error: str) -> dict[str, Any]:
    return {
        "provider": provider,
        "enabled": False,
        "mode": "local",
        "error": error,
    }
