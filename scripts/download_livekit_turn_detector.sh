#!/usr/bin/env bash
# Pre-download livekit/turn-detector (multilingual ONNX) for offline / slow HF access.
#
# Usage:
#   bash scripts/download_livekit_turn_detector.sh
#   HF_ENDPOINT=https://hf-mirror.com bash scripts/download_livekit_turn_detector.sh
#   LIVEKIT_TURN_MODEL_DIR=./models/livekit-turn-detector bash scripts/download_livekit_turn_detector.sh
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

LOCAL_DIR="${LIVEKIT_TURN_MODEL_DIR:-$ROOT/models/livekit-turn-detector}"
REVISION="v0.4.1-intl"
REPO="livekit/turn-detector"

if [[ -f "$LOCAL_DIR/onnx/model_q8.onnx" && -f "$LOCAL_DIR/languages.json" ]]; then
  echo "Model already present under $LOCAL_DIR"
else
  mkdir -p "$LOCAL_DIR"
  if command -v huggingface-cli >/dev/null 2>&1; then
    echo "Downloading $REPO ($REVISION) -> $LOCAL_DIR"
    huggingface-cli download "$REPO" --revision "$REVISION" --local-dir "$LOCAL_DIR"
  elif command -v hf >/dev/null 2>&1; then
    echo "Downloading $REPO ($REVISION) -> $LOCAL_DIR"
    hf download "$REPO" --revision "$REVISION" --local-dir "$LOCAL_DIR"
  else
    echo "Using Python huggingface_hub snapshot_download"
    HF_ENDPOINT="${HF_ENDPOINT:-https://hf-mirror.com}" python3 - <<PY
from pathlib import Path
from huggingface_hub import snapshot_download
import os
dest = Path("$LOCAL_DIR")
dest.mkdir(parents=True, exist_ok=True)
endpoint = (os.environ.get("HF_ENDPOINT") or "").strip() or None
kwargs = dict(
    repo_id="$REPO",
    revision="$REVISION",
    local_dir=str(dest),
)
if endpoint:
    kwargs["endpoint"] = endpoint.rstrip("/")
snapshot_download(**kwargs)
print("Downloaded to", dest)
PY
  fi
fi

export LIVEKIT_TURN_MODEL_DIR="$LOCAL_DIR"
export LIVEKIT_TURN_LOCAL_ONLY="${LIVEKIT_TURN_LOCAL_ONLY:-1}"
python3 - <<'PY'
from backend.vad.livekit_eou_model import get_livekit_eou_model

get_livekit_eou_model()
print("livekit/turn-detector loaded OK from LIVEKIT_TURN_MODEL_DIR")
PY

echo ""
echo "Add to .env:"
echo "  LIVEKIT_TURN_MODEL_DIR=$LOCAL_DIR"
echo "  LIVEKIT_TURN_LOCAL_ONLY=1"
