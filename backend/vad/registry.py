from __future__ import annotations

import logging
from typing import Any

from backend.duplug_vad_client import duplug_server_url
from backend.duplug_vad_local import duplug_local_config_path, duplug_local_enabled
from backend.vad.config import duplug_enabled_for_provider, vad_provider
from backend.vad.duplug_backend import DuplugBackend, get_duplug_local_manager, init_duplug_local_manager

LOG = logging.getLogger(__name__)


def create_vad_backend():
    provider = vad_provider()
    if provider == "smart_turn_v3":
        try:
            from backend.vad.smart_turn_v3 import SmartTurnV3Backend

            return SmartTurnV3Backend()
        except Exception as exc:  # noqa: BLE001
            LOG.warning("Smart Turn v3 init failed: %s", exc)
            return None
    if provider == "duplug" and duplug_enabled_for_provider():
        local = get_duplug_local_manager()
        if local is None and duplug_local_enabled():
            local = init_duplug_local_manager(duplug_local_config_path())
        return DuplugBackend(local_manager=local, remote_url=duplug_server_url())
    LOG.warning("Unknown or disabled VAD provider: %s", provider)
    return None


def vad_status_on_init_failure(provider: str, error: str) -> dict[str, Any]:
    return {
        "provider": provider,
        "enabled": False,
        "mode": "local",
        "error": error,
    }
