# 在【构建机】上生成 Windows 离线安装包（免安装 ZIP；可选 Inno Setup / EXE 安装器）
# 用法： powershell -ExecutionPolicy Bypass -File scripts\build_windows.ps1
$ErrorActionPreference = "Stop"
$ROOT = Split-Path -Parent $PSScriptRoot
Set-Location $ROOT
$VENV = Join-Path $ROOT ".venv"
$VERSION = "1.0.0"

Write-Host "=== 1/5 依赖（仅构建机） ==="
if (-not (Test-Path (Join-Path $VENV "Scripts\python.exe"))) { python -m venv $VENV }
$PY = Join-Path $VENV "Scripts\python.exe"
& $PY -m pip install -q --upgrade pip
& $PY -m pip install -q -r requirements-windows.txt -r requirements-build.txt

Write-Host "=== 2/5 图标与模型完整性 ==="
$env:QT_QPA_PLATFORM = "offscreen"
& $PY scripts/make_icon.py
& $PY scripts/verify_offline.py --check-models

Write-Host "=== 3/5 单元测试 ==="
$env:SAYO_DATA = "$env:TEMP\qt-build-test"
$PYTEST_TMP = Join-Path $env:TEMP ("qt-pytest-" + [guid]::NewGuid().ToString("N"))
& $PY -m pytest -q tests --basetemp $PYTEST_TMP -p no:cacheprovider

Write-Host "=== 4/5 PyInstaller 打包（onedir，模型随包） ==="
& $PY -m PyInstaller --noconfirm --distpath "$ROOT\dist" --workpath "$ROOT\build" packaging\sayo.spec
$OUT = Join-Path $ROOT "dist\sayo"
if (-not (Test-Path (Join-Path $OUT "sayo.exe"))) { throw "打包失败：未生成 sayo.exe" }

Write-Host "=== 5/5 生成离线 ZIP / 可选安装器 ==="
$ZIP = Join-Path $ROOT "dist\sayo-$VERSION-windows-x64.zip"
if (Test-Path $ZIP) { Remove-Item $ZIP -Force }
Compress-Archive -Path $OUT -DestinationPath $ZIP -CompressionLevel Optimal

# 若构建机装有 Inno Setup，可额外生成 setup.exe（同样完全离线）
$ISCC = "C:\Program Files (x86)\Inno Setup 6\ISCC.exe"
if (Test-Path $ISCC) {
  & $ISCC "packaging\windows.iss"
  Write-Host "已生成 Inno Setup 安装器"
} else {
  Write-Host "未检测到 Inno Setup，已提供免安装 ZIP（解压即用，长期离线）"
}
Write-Host "完成：$ZIP"
