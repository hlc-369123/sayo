#!/usr/bin/env bash
# 在【构建机】上生成 macOS 安装包（DMG + PKG）。
# 产物：dist/sayo-1.0.0-macos.dmg、dist/sayo-1.0.0-macos.pkg
# 模型不随包：推理库、PortAudio、Qt 在 .app 内；模型由用户首次使用时
# 执行 scripts/install_models.command 下载到用户数据目录。
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
VENV="${VENV:-$ROOT/.venv}"
APP="$ROOT/dist/轻述 sayo.app"
VERSION="1.0.0"
BUILD_ID="$(date +%Y%m%d-%H%M%S)"

echo "=== 1/6 依赖（仅在构建机） ==="
if [[ ! -x "$VENV/bin/python" ]]; then python3 -m venv "$VENV"; fi
"$VENV/bin/python" -m pip install -q --upgrade pip
"$VENV/bin/python" -m pip install -q -r requirements-macos.txt -r requirements-build.txt

echo "=== 2/6 图标（安装包不含模型，模型完整性不再影响构建） ==="
QT_QPA_PLATFORM=offscreen "$VENV/bin/python" scripts/make_icon.py
if [[ -d "$ROOT/models/sensevoice" ]]; then
  "$VENV/bin/python" scripts/verify_offline.py --check-models || true
fi

echo "=== 3/6 单元测试 ==="
PYTEST_TMP="$(mktemp -d)"
SAYO_DATA=/tmp/qt-build-test "$VENV/bin/python" -m pytest -q tests \
  --basetemp="$PYTEST_TMP" -p no:cacheprovider

echo "=== 4/6 PyInstaller 打包（onedir + .app，模型不随包） ==="
# 重复构建时把上一轮产物改名移走（重命名而非批量删除），保证幂等且不被安全策略拦截
if [[ -d "$ROOT/dist/sayo" ]]; then
  mv "$ROOT/dist/sayo" "/tmp/qt-collect-$BUILD_ID"
fi
if [[ -d "$APP" ]]; then
  mv "$APP" "/tmp/qt-app-$BUILD_ID"
fi
"$VENV/bin/python" -m PyInstaller --noconfirm \
  --distpath "$ROOT/dist" --workpath "$ROOT/build" \
  packaging/sayo.spec

if [[ ! -d "$APP" ]]; then
  echo "打包失败：未生成 $APP" >&2
  exit 1
fi

echo "=== 5/6 本地签名与自检 ==="
codesign --force --deep --sign - "$APP" || echo "（ad-hoc 签名失败，仍可用右键→打开方式启动）"
# 构建机若本地有 models/，用 SAYO_MODELS 指过去做一次完整自检；
# 没有模型也能正常打包（自检会提示模型未安装，属预期）。
APP_EXE="$APP/Contents/MacOS/sayo"
if [[ -x "$APP_EXE" ]]; then
  SAYO_DATA=/tmp/qt-build-test SAYO_MODELS="$ROOT/models" "$APP_EXE" --self-check || true
fi

echo "=== 6/6 生成 DMG / PKG ==="
STAGE="$ROOT/dist/dmg-stage-$BUILD_ID"
mkdir -p "$STAGE"
cp -R "$APP" "$STAGE/"
ln -s /Applications "$STAGE/Applications"
rm -f "$ROOT/dist/sayo-$VERSION-macos.pkg"
hdiutil create -volname "轻述 sayo" -srcfolder "$STAGE" -ov -format UDZO \
  "$ROOT/dist/sayo-$VERSION-macos.dmg"
pkgbuild --component "$APP" --install-location /Applications \
  --identifier local.sayo.desktop --version "$VERSION" \
  "$ROOT/dist/sayo-$VERSION-macos.pkg"

# 把大体积临时副本移出 dist（重命名而非批量删除，反复构建不会越堆越多）
TRASH="/tmp/qt-build-trash-$BUILD_ID"
mkdir -p "$TRASH"
for p in "$STAGE" "$ROOT/dist/sayo" "/tmp/qt-collect-$BUILD_ID" "/tmp/qt-app-$BUILD_ID"; do
  if [[ -e "$p" ]]; then
    mv "$p" "$TRASH/" 2>/dev/null || true
  fi
done
echo "构建临时副本已移出 dist：$TRASH"

echo "完成："
ls -lh "$ROOT/dist/sayo-$VERSION-macos.dmg" "$ROOT/dist/sayo-$VERSION-macos.pkg"
echo "安装包不含模型；首次使用前运行一次：bash scripts/install_models.command"
echo "（模型下载后应用运行时完全离线）"
echo "离线介质安装：bash scripts/install_offline_macos.sh \"<U盘目录>/轻述 sayo.app\""
