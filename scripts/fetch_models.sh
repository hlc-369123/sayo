#!/usr/bin/env bash
# 【构建机 / 开发者】把模型下载到仓库 models/，用于本地开发或从源码运行。
# 安装包不再内含模型；最终用户请改用 scripts/install_models.command（下载到用户数据目录）。
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
MODELS="$ROOT/models"
HF_MIRROR="${HF_MIRROR:-https://hf-mirror.com}"
GH_MIRROR="${GH_MIRROR:-https://gh-proxy.com/https://github.com}"
REPO="csukuangfj/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17"
WITH_WHISPER=0
FORCE=0
LITE=0

for arg in "$@"; do
  case "$arg" in
    --with-whisper) WITH_WHISPER=1 ;;
    --lite) LITE=1 ;;
    --force) FORCE=1 ;;
    *) echo "未知参数: $arg"; exit 2 ;;
  esac
done

mkdir -p "$MODELS/sensevoice"
SV="$MODELS/sensevoice/model.int8.onnx"
FP32="$MODELS/sensevoice/model.onnx"
TK="$MODELS/sensevoice/tokens.txt"
VAD="$MODELS/silero_vad.onnx"

need() { [[ ! -f "$1" ]] || [[ "$FORCE" == "1" ]]; }

echo ">>> [1/4] SenseVoice fp32 全精度（默认档，准确率更高）"
if [[ "$LITE" == "1" ]]; then
  echo "    --lite：跳过 fp32，安装包只带 int8；应用会自动回退 int8"
elif need "$FP32"; then
  curl -fL --retry 3 -o "$FP32" "$HF_MIRROR/$REPO/resolve/main/model.onnx"
else
  echo "    已存在，跳过（--force 可强制重下）"
fi

echo ">>> [2/4] SenseVoice int8（极速 / 小体积档）"
if need "$SV"; then
  curl -fL --retry 3 -o "$SV" "$HF_MIRROR/$REPO/resolve/main/model.int8.onnx"
fi
if need "$TK"; then
  curl -fL --retry 3 -o "$TK" "$HF_MIRROR/$REPO/resolve/main/tokens.txt"
fi

echo ">>> [3/4] Silero VAD"
if need "$VAD"; then
  curl -fL --retry 3 -o "$VAD" \
    "$GH_MIRROR/k2-fsa/sherpa-onnx/releases/download/asr-models/silero_vad.onnx"
fi

if [[ "$WITH_WHISPER" == "1" ]]; then
  echo ">>> [4/4] 可选 Whisper multilingual（100+ 语种）"
  TMP="$(mktemp -d)"
  curl -fL --retry 3 -o "$TMP/whisper.tar.bz2" \
    "$GH_MIRROR/k2-fsa/sherpa-onnx/releases/download/asr-models/sherpa-onnx-whisper-base.tar.bz2"
  tar -xjf "$TMP/whisper.tar.bz2" -C "$TMP"
  mkdir -p "$MODELS/whisper"
  cp "$(find "$TMP" -name '*encoder*.onnx' | head -1)" "$MODELS/whisper/encoder.onnx"
  cp "$(find "$TMP" -name '*decoder*.onnx' | head -1)" "$MODELS/whisper/decoder.onnx"
  cp "$(find "$TMP" -name '*tokens*.txt'  | head -1)" "$MODELS/whisper/tokens.txt"
  rm -rf "$TMP"
  echo "    Whisper 已放入 models/whisper/；安装包体积会相应增大"
else
  echo ">>> [4/4] 跳过 Whisper（默认；需要 100+ 语种时用 --with-whisper）"
fi

echo ">>> 校验"
python3 "$ROOT/scripts/verify_offline.py" --check-models
echo "完成。模型只用于本地开发 / 源码运行；安装包不含模型，无需把它们打进 .app。"
