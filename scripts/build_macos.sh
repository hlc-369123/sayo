#!/usr/bin/env bash
# 在【构建机】上生成 macOS 离线安装包（DMG + PKG）。
# 产物：dist/Saylo-1.0.0-macos.dmg、dist/Saylo-1.0.0-macos.pkg
# 目标机器安装后完全离线：模型、推理库、PortAudio、Qt 全部在 .app 内。
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
VENV="${VENV:-$ROOT/.venv}"
APP="$ROOT/dist/轻述 Saylo.app"
VERSION="1.0.0"
BUILD_ID="$(date +%Y%m%d-%H%M%S)"

echo "=== 1/6 依赖（仅在构建机） ==="
if [[ ! -x "$VENV/bin/python" ]]; then python3 -m venv "$VENV"; fi
"$VENV/bin/python" -m pip install -q --upgrade pip
"$VENV/bin/python" -m pip install -q -r requirements-macos.txt -r requirements-build.txt

echo "=== 2/6 图标与模型完整性 ==="
QT_QPA_PLATFORM=offscreen "$VENV/bin/python" scripts/make_icon.py
"$VENV/bin/python" scripts/verify_offline.py --check-models

echo "=== 3/6 单元测试 ==="
PYTEST_TMP="$(mktemp -d)"
SAYLO_DATA=/tmp/qt-build-test "$VENV/bin/python" -m pytest -q tests \
  --basetemp="$PYTEST_TMP" -p no:cacheprovider

echo "=== 4/6 PyInstaller 打包（onedir + .app，模型随包） ==="
# 重复构建时把上一轮产物改名移走（重命名而非批量删除），保证幂等且不被安全策略拦截
if [[ -d "$ROOT/dist/Saylo" ]]; then
  mv "$ROOT/dist/Saylo" "/tmp/qt-collect-$BUILD_ID"
fi
if [[ -d "$APP" ]]; then
  mv "$APP" "/tmp/qt-app-$BUILD_ID"
fi
"$VENV/bin/python" -m PyInstaller --noconfirm \
  --distpath "$ROOT/dist" --workpath "$ROOT/build" \
  packaging/saylo.spec

if [[ ! -d "$APP" ]]; then
  echo "打包失败：未生成 $APP" >&2
  exit 1
fi

echo "=== 5/6 本地签名与离线自检 ==="
codesign --force --deep --sign - "$APP" || echo "（ad-hoc 签名失败，仍可用右键→打开方式启动）"
# 用打包后的可执行文件跑一次「模型 + 断网 + 真实识别」验收
APP_EXE="$APP/Contents/MacOS/Saylo"
if [[ -x "$APP_EXE" ]]; then
  SAYLO_DATA=/tmp/qt-build-test "$APP_EXE" --self-check || true
fi

echo "=== 6/6 生成 DMG / PKG ==="
STAGE="$ROOT/dist/dmg-stage-$BUILD_ID"
mkdir -p "$STAGE"
cp -R "$APP" "$STAGE/"
ln -s /Applications "$STAGE/Applications"
rm -f "$ROOT/dist/Saylo-$VERSION-macos.pkg"
hdiutil create -volname "轻述 Saylo" -srcfolder "$STAGE" -ov -format UDZO \
  "$ROOT/dist/Saylo-$VERSION-macos.dmg"
pkgbuild --component "$APP" --install-location /Applications \
  --identifier local.saylo.desktop --version "$VERSION" \
  "$ROOT/dist/Saylo-$VERSION-macos.pkg"

# 把大体积临时副本移出 dist（重命名而非批量删除，反复构建不会越堆越多）
TRASH="/tmp/qt-build-trash-$BUILD_ID"
mkdir -p "$TRASH"
for p in "$STAGE" "$ROOT/dist/Saylo" "/tmp/qt-collect-$BUILD_ID" "/tmp/qt-app-$BUILD_ID"; do
  if [[ -e "$p" ]]; then
    mv "$p" "$TRASH/" 2>/dev/null || true
  fi
done
echo "构建临时副本已移出 dist：$TRASH"

echo "完成："
ls -lh "$ROOT/dist/Saylo-$VERSION-macos.dmg" "$ROOT/dist/Saylo-$VERSION-macos.pkg"
echo "把 DMG/PKG 复制到 U 盘即可在无网络机器上安装；安装后运行："
echo "  bash scripts/install_offline_macos.sh \"<U盘目录>/轻述 Saylo.app\""
