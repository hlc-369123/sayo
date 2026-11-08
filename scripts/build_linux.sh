#!/usr/bin/env bash
# 在【构建机】上生成 Linux 离线包（tar.gz；可选 AppImage）
# 目标机器需要系统库：libportaudio2、libxcb 系列、libxkbcommon（多数桌面发行版自带）
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
VENV="${VENV:-$ROOT/.venv}"
VERSION="1.0.0"

echo "=== 1/5 依赖（仅构建机） ==="
if [[ ! -x "$VENV/bin/python" ]]; then python3 -m venv "$VENV"; fi
"$VENV/bin/python" -m pip install -q --upgrade pip
"$VENV/bin/python" -m pip install -q -r requirements.txt -r requirements-build.txt

echo "=== 2/5 图标与模型完整性 ==="
QT_QPA_PLATFORM=offscreen "$VENV/bin/python" scripts/make_icon.py
"$VENV/bin/python" scripts/verify_offline.py --check-models

echo "=== 3/5 单元测试 ==="
PYTEST_TMP="$(mktemp -d)"
SAYO_DATA=/tmp/qt-build-test "$VENV/bin/python" -m pytest -q tests \
  --basetemp="$PYTEST_TMP" -p no:cacheprovider

echo "=== 4/5 PyInstaller 打包 ==="
"$VENV/bin/python" -m PyInstaller --noconfirm \
  --distpath "$ROOT/dist" --workpath "$ROOT/build" \
  packaging/sayo.spec

echo "=== 5/5 生成离线 tar.gz / 桌面入口 ==="
OUT="$ROOT/dist/sayo"
DESKTOP="$ROOT/dist/sayo.desktop"
cat > "$DESKTOP" <<'EOF'
[Desktop Entry]
Type=Application
Name=轻述 sayo
Comment=全离线 AI 语音听写键盘
Exec=sayo
Icon=icon
Categories=Utility;Accessibility;
Terminal=false
EOF
tar -czf "$ROOT/dist/sayo-$VERSION-linux-x64.tar.gz" -C "$ROOT/dist" sayo sayo.desktop

if command -v appimagetool >/dev/null 2>&1; then
  echo "检测到 appimagetool，可进一步生成 AppImage（离线单文件）"
else
  echo "未检测到 appimagetool，已提供解压即用的 tar.gz"
fi
ls -lh "$ROOT/dist/sayo-$VERSION-linux-x64.tar.gz"
echo "离线安装：把 tar.gz 复制到目标机器解压，运行 ./sayo/sayo 即可，无需 sudo、无需联网。"
