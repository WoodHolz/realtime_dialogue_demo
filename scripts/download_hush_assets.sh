#!/usr/bin/env bash
# Download Hush (weya-ai/hush) runtime assets for Omni uplink denoising.
# See: https://github.com/pulp-vision/Hush/tree/main/deployment
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LIB_DIR="$ROOT/third_party/hush/lib"
MODEL_DIR="$ROOT/third_party/hush/models"
MODEL_NAME="advanced_dfnet16k_model_best_onnx.tar.gz"

mkdir -p "$LIB_DIR" "$MODEL_DIR"

case "$(uname -s)" in
  Linux*)   LIB_NAME="libweya_nc.so" ;;
  Darwin*)  LIB_NAME="libweya_nc.dylib" ;;
  MINGW*|MSYS*|CYGWIN*) LIB_NAME="weya_nc.dll" ;;
  *) echo "Unsupported platform: $(uname -s)" >&2; exit 1 ;;
esac

LIB_URL="https://raw.githubusercontent.com/pulp-vision/Hush/main/deployment/lib/${LIB_NAME}"
MODEL_URL="https://huggingface.co/weya-ai/hush/resolve/main/onnx/${MODEL_NAME}"

echo "Downloading ${LIB_NAME} ..."
curl -fL --retry 3 --retry-delay 2 -o "$LIB_DIR/$LIB_NAME" "$LIB_URL"

echo "Downloading ${MODEL_NAME} ..."
curl -fL --retry 3 --retry-delay 2 -o "$MODEL_DIR/$MODEL_NAME" "$MODEL_URL"

echo "Done."
ls -lh "$LIB_DIR/$LIB_NAME" "$MODEL_DIR/$MODEL_NAME"
