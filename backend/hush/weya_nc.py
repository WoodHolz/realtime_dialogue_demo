"""Python wrapper for the Weya NC Standalone C library (Hush / DeepFilterNet-SE).

Adapted from pulp-vision/Hush deployment/examples/python/weya_nc.py
https://github.com/pulp-vision/Hush/tree/main/deployment
"""

from __future__ import annotations

import ctypes
import os
import platform
import threading
from pathlib import Path
from typing import Optional

import numpy as np

_CACHE_LOCK = threading.Lock()

_LIB_NAMES = {
    "Linux": "libweya_nc.so",
    "Darwin": "libweya_nc.dylib",
    "Windows": "weya_nc.dll",
}


def _platform_lib_name() -> str:
    return _LIB_NAMES.get(platform.system(), "libweya_nc.so")


def _find_library(explicit: Optional[str | Path]) -> Path:
    if explicit is not None:
        p = Path(explicit).resolve()
        if p.is_dir():
            candidate = p / _platform_lib_name()
            if candidate.exists():
                return candidate
        if p.exists():
            return p
        raise FileNotFoundError(f"Hush library not found: {p}")

    env = (os.environ.get("HUSH_LIB_PATH", "") or os.environ.get("WEYA_NC_LIB_PATH", "") or "").strip()
    if env:
        p = Path(env).expanduser().resolve()
        if p.is_dir():
            candidate = p / _platform_lib_name()
            if candidate.exists():
                return candidate
        if p.exists():
            return p

    root = Path(__file__).resolve().parent.parent.parent
    candidate = root / "third_party" / "hush" / "lib" / _platform_lib_name()
    if candidate.exists():
        return candidate

    raise FileNotFoundError(
        f"Could not find {_platform_lib_name()}. "
        "Run scripts/download_hush_assets.sh or set HUSH_LIB_PATH."
    )


def _find_model(explicit: Optional[str | Path]) -> Path:
    if explicit is not None:
        p = Path(explicit).resolve()
        if p.exists():
            return p
        raise FileNotFoundError(f"Hush ONNX model not found: {p}")

    env = (os.environ.get("HUSH_MODEL_PATH", "") or os.environ.get("WEYA_NC_MODEL_PATH", "") or "").strip()
    if env:
        p = Path(env).expanduser().resolve()
        if p.exists():
            return p

    root = Path(__file__).resolve().parent.parent.parent
    candidate = root / "third_party" / "hush" / "models" / "advanced_dfnet16k_model_best_onnx.tar.gz"
    if candidate.exists():
        return candidate

    raise FileNotFoundError(
        "Could not find advanced_dfnet16k_model_best_onnx.tar.gz. "
        "Run scripts/download_hush_assets.sh or set HUSH_MODEL_PATH."
    )


_LIB_CACHE: dict[str, ctypes.CDLL] = {}


def _declare_api(lib: ctypes.CDLL) -> None:
    lib.weya_nc_model_load.argtypes = []
    lib.weya_nc_model_load.restype = ctypes.c_void_p

    lib.weya_nc_model_load_from_path.argtypes = [ctypes.c_char_p]
    lib.weya_nc_model_load_from_path.restype = ctypes.c_void_p

    lib.weya_nc_model_free.argtypes = [ctypes.c_void_p]
    lib.weya_nc_model_free.restype = None

    lib.weya_nc_session_create.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_float]
    lib.weya_nc_session_create.restype = ctypes.c_void_p

    lib.weya_nc_session_free.argtypes = [ctypes.c_void_p]
    lib.weya_nc_session_free.restype = None

    lib.weya_nc_get_frame_length.argtypes = [ctypes.c_void_p]
    lib.weya_nc_get_frame_length.restype = ctypes.c_size_t

    lib.weya_nc_get_sample_rate.argtypes = [ctypes.c_void_p]
    lib.weya_nc_get_sample_rate.restype = ctypes.c_size_t

    lib.weya_nc_get_input_sample_rate.argtypes = [ctypes.c_void_p]
    lib.weya_nc_get_input_sample_rate.restype = ctypes.c_size_t

    float_ptr = ctypes.POINTER(ctypes.c_float)
    lib.weya_nc_process_frame.argtypes = [ctypes.c_void_p, float_ptr, float_ptr]
    lib.weya_nc_process_frame.restype = ctypes.c_float

    lib.weya_nc_reset.argtypes = [ctypes.c_void_p]
    lib.weya_nc_reset.restype = None


def _load_library(lib_file: Path) -> ctypes.CDLL:
    key = str(lib_file)
    with _CACHE_LOCK:
        lib = _LIB_CACHE.get(key)
        if lib is None:
            lib = ctypes.CDLL(key)
            _declare_api(lib)
            _LIB_CACHE[key] = lib
        return lib


class WeyaModel:
    """Loaded ONNX model; spawn one session per concurrent audio stream."""

    def __init__(
        self,
        lib_path: Optional[str | Path] = None,
        model_path: Optional[str | Path] = None,
    ) -> None:
        lib_file = _find_library(lib_path)
        model_file = _find_model(model_path)
        self._lib = _load_library(lib_file)
        self._model = self._lib.weya_nc_model_load_from_path(str(model_file).encode("utf-8"))
        if not self._model:
            raise RuntimeError(f"Failed to load Hush model from {model_file}")

    def create_session(self, sample_rate: int = 16000, atten_lim_db: float = 100.0) -> "WeyaNC":
        if not getattr(self, "_model", None):
            raise RuntimeError("Hush model has been closed")
        return WeyaNC(_model=self, sample_rate=sample_rate, atten_lim_db=atten_lim_db)

    def close(self) -> None:
        if getattr(self, "_model", None):
            self._lib.weya_nc_model_free(self._model)
            self._model = None

    def __enter__(self) -> "WeyaModel":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def __del__(self) -> None:
        self.close()


class WeyaNC:
    """One streaming denoising session (not reentrant; one per uplink stream)."""

    def __init__(
        self,
        lib_path: Optional[str | Path] = None,
        model_path: Optional[str | Path] = None,
        sample_rate: int = 16000,
        atten_lim_db: float = 100.0,
        _model: Optional[WeyaModel] = None,
    ) -> None:
        if _model is not None:
            self._model_owner = None
            self._model = _model
        else:
            self._model_owner = WeyaModel(lib_path=lib_path, model_path=model_path)
            self._model = self._model_owner

        self._lib = self._model._lib
        self._lock = threading.Lock()
        self._session = self._lib.weya_nc_session_create(
            self._model._model, sample_rate, ctypes.c_float(atten_lim_db)
        )
        if not self._session:
            raise RuntimeError("Failed to create Hush denoising session")

        self._sample_rate = int(self._lib.weya_nc_get_input_sample_rate(self._session))
        self._frame_length = int(self._lib.weya_nc_get_frame_length(self._session))
        self._buf_in = np.zeros(self._frame_length, dtype=np.float32)
        self._buf_out = np.zeros(self._frame_length, dtype=np.float32)

    @property
    def frame_length(self) -> int:
        return self._frame_length

    @property
    def sample_rate(self) -> int:
        return self._sample_rate

    def process_frame(self, frame: np.ndarray) -> np.ndarray:
        if len(frame) != self._frame_length:
            raise ValueError(f"Expected {self._frame_length} samples, got {len(frame)}")

        input_dtype = frame.dtype
        float_ptr = ctypes.POINTER(ctypes.c_float)
        with self._lock:
            if input_dtype == np.int16:
                self._buf_in[:] = frame.astype(np.float32) / 32768.0
            else:
                self._buf_in[:] = frame.astype(np.float32)

            self._lib.weya_nc_process_frame(
                self._session,
                self._buf_in.ctypes.data_as(float_ptr),
                self._buf_out.ctypes.data_as(float_ptr),
            )

            if input_dtype == np.int16:
                return (self._buf_out * 32768.0).clip(-32768, 32767).astype(np.int16)
            return self._buf_out.copy()

    def reset(self) -> None:
        with self._lock:
            if self._session:
                self._lib.weya_nc_reset(self._session)

    def close(self) -> None:
        lock = getattr(self, "_lock", None)
        if lock is not None:
            with lock:
                if getattr(self, "_session", None):
                    self._lib.weya_nc_session_free(self._session)
                    self._session = None
                if getattr(self, "_model_owner", None) is not None:
                    self._model_owner.close()
                    self._model_owner = None
                    self._model = None

    def __enter__(self) -> "WeyaNC":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def __del__(self) -> None:
        self.close()
