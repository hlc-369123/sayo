#!/usr/bin/env bash
# 离线安装：从 U 盘 / 内网介质把 .app 装到本机 /Applications，全程不联网。
# 用法： bash install_offline_macos.sh "/Volumes/USB/轻述 Saylo.app" [--launch]
#   --launch = 装完后跑一次离线自检并启动应用，方便立即试用
set -euo pipefail
SRC="${1:-./轻述 Saylo.app}"
LAUNCH="${2:-}"
DEST="/Applications/轻述 Saylo.app"

if [[ ! -d "$SRC" ]]; then echo "找不到应用：$SRC" >&2; exit 1; fi
echo "安装到：$DEST"
if [[ -d "$DEST" ]]; then
  echo "检测到旧版本，改名为 .old（不会删除用户数据）"
  mv "$DEST" "${DEST}.old.$(date +%s)"
fi
# 与 /Applications 同一卷时直接移动，避免重复占用 1.2 GB 磁盘；U 盘等跨卷介质才复制
SAME_VOLUME=0
if SRC_DEV="$(stat -f %d "$SRC" 2>/dev/null)" && DST_DEV="$(stat -f %d /Applications 2>/dev/null)" \
   && [[ "$SRC_DEV" == "$DST_DEV" ]]; then
  SAME_VOLUME=1
fi
if [[ "$SAME_VOLUME" == "1" ]]; then
  echo "同卷安装：直接移动（不额外占用磁盘）"
  mv "$SRC" "$DEST"
else
  echo "跨卷介质：复制到 /Applications"
  cp -R "$SRC" "$DEST"
fi

# 去除离线介质带来的 quarantine 标记，避免 Gatekeeper 拦截未公证应用
xattr -dr com.apple.quarantine "$DEST" 2>/dev/null || true
codesign --force --deep --sign - "$DEST" 2>/dev/null || true

# 清掉历史安装留下的旧「辅助功能」条目（每次构建签名都变，旧条目与新二进制不匹配，
# 会出现「开关明明开着却始终未授权」的假象）。失败不影响安装，只提示手动清理。
if ! tccutil reset Accessibility local.saylo.desktop >/dev/null 2>&1; then
  echo "提示：未能自动清理旧授权条目。若 系统设置 → 辅助功能 里有多条「轻述 Saylo」，"
  echo "      请把旧条目选中按 − 删除，再用 ➕ 添加 \"$DEST\" 并打开开关。"
fi

echo "全局快捷键为系统级注册，无需「输入监控」授权，覆盖安装也不会失效。"
echo "首次启动只需授予：麦克风（录音）、辅助功能（自动粘贴到输入框）。"
echo "  未授予辅助功能时文字仍会写入剪贴板；设置 → 权限与隐私 可一键申请"

if [[ "$LAUNCH" == "--launch" ]]; then
  EXE="$DEST/Contents/MacOS/Saylo"
  if [[ -x "$EXE" ]]; then
    echo "=== 安装后离线自检 ==="
    SAYLO_DATA="${SAYLO_DATA:-$HOME/Library/Application Support/Saylo}" "$EXE" --self-check || true
    echo "=== 本机推理基准（CPU/GPU × fp32/int8） ==="
    "$EXE" --benchmark || true
    echo "=== 启动应用 ==="
    open "$DEST"
  fi
else
  echo "安装完成。可通过 open \"$DEST\" 启动，无账号、无激活、无联网检查。"
fi
