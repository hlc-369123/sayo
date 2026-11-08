#!/usr/bin/env bash
# 轻述 sayo · 首次使用模型下载器（macOS 双击即可运行）
#
# 安装包不含语音模型，只需运行本脚本一次，把模型下载到用户数据目录：
#   ~/Library/Application Support/sayo/models/
# 下载完成后应用运行时完全离线，不会再发起任何网络请求。
#
# 用法：
#   双击 install_models.command        只下 int8（约 240 MB，够用，识别自动回退 int8）
#   ./install_models.command --full    额外下 fp32 全精度（约 940 MB，轻声/噪声下更稳）
# 可用环境变量 HF_MIRROR / GH_MIRROR 换下载源；支持断点续传，重复运行会跳过已校验文件。
set -euo pipefail

MODELS="$HOME/Library/Application Support/sayo/models"
HF_MIRROR="${HF_MIRROR:-https://hf-mirror.com}"
GH_MIRROR="${GH_MIRROR:-https://gh-proxy.com/https://github.com}"
REPO="csukuangfj/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17"
FULL=0

for arg in "$@"; do
  case "$arg" in
    --full) FULL=1 ;;
    *) echo "未知参数: $arg（可用：--full）" >&2; exit 2 ;;
  esac
done

echo "════════════════════════════════════════════"
echo " 轻述 sayo · 模型安装（只需一次，之后永久离线）"
echo " 目标目录：$MODELS"
echo "════════════════════════════════════════════"
mkdir -p "$MODELS/sensevoice"

sha256_of() {
  if command -v shasum >/dev/null 2>&1; then
    shasum -a 256 "$1" | cut -d' ' -f1
  else
    sha256sum "$1" | cut -d' ' -f1
  fi
}

size_of() { wc -c < "$1" | tr -d ' '; }

# download <相对路径> <sha256> <字节数> <URL>
download() {
  local rel="$1" sum="$2" size="$3" url="$4"
  local dest="$MODELS/$rel"
  if [[ -f "$dest" ]] && [[ "$(size_of "$dest")" == "$size" ]] \
     && [[ "$(sha256_of "$dest")" == "$sum" ]]; then
    echo "✓ 已存在且校验通过：$rel"
    return 0
  fi
  echo "↓ 下载 $rel （约 $((size / 1048576)) MB）"
  echo "  $url"
  mkdir -p "$(dirname "$dest")"
  if ! curl -fL --retry 3 --retry-delay 2 -C - -o "$dest" "$url"; then
    echo "✗ 下载失败：$rel" >&2
    echo "  可换源后重试，例如：HF_MIRROR=https://huggingface.co ./install_models.command" >&2
    return 1
  fi
  local actual
  actual="$(sha256_of "$dest")"
  if [[ "$actual" != "$sum" ]]; then
    echo "✗ 校验失败：$rel" >&2
    echo "  期望 $sum" >&2
    echo "  实际 $actual" >&2
    echo "  请删除该文件后重跑（支持断点续传）。" >&2
    return 1
  fi
  echo "✓ 校验通过：$rel"
}

download "sensevoice/model.int8.onnx" \
  "c71f0ce00bec95b07744e116345e33d8cbbe08cef896382cf907bf4b51a2cd51" \
  239233841 \
  "$HF_MIRROR/$REPO/resolve/main/model.int8.onnx"

download "sensevoice/tokens.txt" \
  "f449eb28dc567533d7fa59be34e2abca8784f771850c78a47fb731a31429a1dc" \
  315894 \
  "$HF_MIRROR/$REPO/resolve/main/tokens.txt"

download "silero_vad.onnx" \
  "9e2449e1087496d8d4caba907f23e0bd3f78d91fa552479bb9c23ac09cbb1fd6" \
  643854 \
  "$GH_MIRROR/k2-fsa/sherpa-onnx/releases/download/asr-models/silero_vad.onnx"

if [[ "$FULL" == "1" ]]; then
  download "sensevoice/model.onnx" \
    "977016bd9c79f9eb343430b5cc305e07ab64d5212dff41b0dcfa1694bee9a8cb" \
    937617178 \
    "$HF_MIRROR/$REPO/resolve/main/model.onnx"
else
  echo
  echo "提示：默认只安装 int8（约 240 MB），应用会自动使用该模型。"
  echo "      想要 fp32 全精度：./install_models.command --full"
fi

echo
echo "════════════════════════════════════════════"
echo " 完成。模型目录：$MODELS"
echo " 现在可以启动「轻述 sayo」；应用运行时完全离线。"
echo "════════════════════════════════════════════"
