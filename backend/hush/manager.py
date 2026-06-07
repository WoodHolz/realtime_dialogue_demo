from __future__ import annotations

import logging
import threading

from backend.hush.config import hush_atten_lim_db, hush_enabled, hush_lib_path, hush_model_path
from backend.hush.session import HushDenoiseSession
from backend.hush.weya_nc import WeyaModel

LOG = logging.getLogger(__name__)

_MODEL: WeyaModel | None = None
_MODEL_ERROR: str = ""
_LOCK = threading.Lock()
_SAMPLE_RATE = 16000


def init_hush_manager() -> WeyaModel | None:
    """Load shared Hush model once at process startup."""
    global _MODEL, _MODEL_ERROR
    if not hush_enabled():
        return None
    with _LOCK:
        if _MODEL is not None:
            return _MODEL
        if _MODEL_ERROR:
            return None
        try:
            _MODEL = WeyaModel(lib_path=hush_lib_path(), model_path=hush_model_path())
            probe = _MODEL.create_session(sample_rate=_SAMPLE_RATE, atten_lim_db=hush_atten_lim_db())
            frame_len = probe.frame_length
            probe.close()
            LOG.info(
                "Hush denoiser ready (frame=%s samples @ %s Hz, atten_lim_db=%s)",
                frame_len,
                _SAMPLE_RATE,
                hush_atten_lim_db(),
            )
            return _MODEL
        except Exception as exc:
            _MODEL_ERROR = str(exc)
            LOG.warning("Hush denoiser init failed: %s", exc)
            return None


def shutdown_hush_manager() -> None:
    global _MODEL, _MODEL_ERROR
    with _LOCK:
        if _MODEL is not None:
            try:
                _MODEL.close()
            except Exception:
                pass
            _MODEL = None
        _MODEL_ERROR = ""


def hush_manager_error() -> str:
    return _MODEL_ERROR


def create_hush_session() -> HushDenoiseSession | None:
    """Create one per-connection denoising session."""
    model = _MODEL or init_hush_manager()
    if model is None:
        return None
    nc = model.create_session(sample_rate=_SAMPLE_RATE, atten_lim_db=hush_atten_lim_db())
    return HushDenoiseSession(nc)
