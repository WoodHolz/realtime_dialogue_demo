from __future__ import annotations

import json
import os
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_DEFAULT_LIB_DIR = _PROJECT_ROOT / "third_party" / "hush" / "lib"
_DEFAULT_MODEL = (
    _PROJECT_ROOT / "third_party" / "hush" / "models" / "advanced_dfnet16k_model_best_onnx.tar.gz"
)


def _load_omni_json() -> dict:
    try:
        cfg_path = _PROJECT_ROOT / "config.json"
        if cfg_path.exists():
            with cfg_path.open("r", encoding="utf-8") as f:
                data = json.load(f)
            omni = data.get("omni")
            if isinstance(omni, dict):
                return omni
    except Exception:
        pass
    return {}


def _load_hush_json() -> dict:
    hush = _load_omni_json().get("hush")
    return hush if isinstance(hush, dict) else {}


def _env_bool(name: str) -> bool | None:
    raw = (os.environ.get(name, "") or "").strip().lower()
    if not raw:
        return None
    return raw in {"1", "true", "yes", "on"}


def hush_enabled() -> bool:
    """Whether Omni uplink should pass through Hush denoising."""
    env = _env_bool("OMNI_HUSH_ENABLED")
    if env is not None:
        return env
    env = _env_bool("HUSH_ENABLED")
    if env is not None:
        return env
    cfg = _load_hush_json()
    if "enabled" in cfg:
        return bool(cfg.get("enabled"))
    return False


def hush_lib_path() -> Path | None:
    env = (os.environ.get("HUSH_LIB_PATH", "") or os.environ.get("WEYA_NC_LIB_PATH", "") or "").strip()
    if env:
        return Path(env).expanduser().resolve()
    cfg = _load_hush_json()
    p = cfg.get("lib_path")
    if p:
        return Path(str(p)).expanduser().resolve()
    name = "libweya_nc.so"
    import platform

    names = {
        "Darwin": "libweya_nc.dylib",
        "Windows": "weya_nc.dll",
    }
    name = names.get(platform.system(), name)
    candidate = _DEFAULT_LIB_DIR / name
    return candidate if candidate.exists() else _DEFAULT_LIB_DIR / "libweya_nc.so"


def hush_model_path() -> Path | None:
    env = (os.environ.get("HUSH_MODEL_PATH", "") or os.environ.get("WEYA_NC_MODEL_PATH", "") or "").strip()
    if env:
        return Path(env).expanduser().resolve()
    cfg = _load_hush_json()
    p = cfg.get("model_path")
    if p:
        return Path(str(p)).expanduser().resolve()
    return _DEFAULT_MODEL


def hush_atten_lim_db() -> float:
    env = (os.environ.get("HUSH_ATTEN_LIM_DB", "") or "").strip()
    if env:
        try:
            return float(env)
        except ValueError:
            pass
    cfg = _load_hush_json()
    if cfg.get("atten_lim_db") is not None:
        try:
            return float(cfg["atten_lim_db"])
        except (TypeError, ValueError):
            pass
    return 100.0


def hush_status_payload(*, ready: bool, error: str = "") -> dict[str, object]:
    lib = hush_lib_path()
    model = hush_model_path()
    return {
        "enabled": hush_enabled(),
        "ready": ready,
        "provider": "weya-ai/hush",
        "lib_path": str(lib) if lib else "",
        "model_path": str(model) if model else "",
        "atten_lim_db": hush_atten_lim_db(),
        "error": error,
    }
