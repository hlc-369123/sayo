# 在【构建机】上生成 Windows 离线安装包（免安装 ZIP；可选 Inno Setup / EXE 安装器）
# 用法： powershell -ExecutionPolicy Bypass -File scripts\build_windows.ps1
$ErrorActionPreference = "Stop"
$ROOT = Split-Path -Parent $PSScriptRoot
Set-Location $ROOT
$VENV = Join-Path $ROOT ".venv"
# 版本号唯一来源：app/config.py 的 APP_VERSION（安装包名、安装器版本、应用内版本都用它）
$VERSION = (Select-String -Path (Join-Path $ROOT "app\config.py") -Pattern '^APP_VERSION = "(.+)"').Matches[0].Groups[1].Value
if (-not $VERSION) { throw "无法从 app/config.py 读取 APP_VERSION" }
Write-Host "版本：$VERSION"

Write-Host "=== 1/5 依赖（仅构建机） ==="
# CI 上已由 actions/setup-python 装好解释器与依赖，用 SAYO_PYTHON 指定即可跳过建 venv
$PY = $env:SAYO_PYTHON
if ($PY) {
  Write-Host "使用外部解释器：$PY"
} else {
  if (-not (Test-Path (Join-Path $VENV "Scripts\python.exe"))) { python -m venv $VENV }
  $PY = Join-Path $VENV "Scripts\python.exe"
}
& $PY -m pip install -q --upgrade pip
& $PY -m pip install -q -r requirements-windows.txt -r requirements-build.txt

Write-Host "=== 2/5 图标与模型完整性 ==="
$env:QT_QPA_PLATFORM = "offscreen"
& $PY scripts/make_icon.py
# CI 机器上没有语音模型，用 SAYO_SKIP_MODEL_CHECK=1 跳过这一步
if ($env:SAYO_SKIP_MODEL_CHECK -ne "1") {
  & $PY scripts/verify_offline.py --check-models
}

Write-Host "=== 3/5 单元测试 ==="
# CI 上用 SAYO_SKIP_TESTS=1 跳过，测试由 ci.yml 单独把关
if ($env:SAYO_SKIP_TESTS -ne "1") {
  $env:SAYO_DATA = "$env:TEMP\qt-build-test"
  $PYTEST_TMP = Join-Path $env:TEMP ("qt-pytest-" + [guid]::NewGuid().ToString("N"))
  & $PY -m pytest -q tests --basetemp $PYTEST_TMP -p no:cacheprovider
  if ($LASTEXITCODE -ne 0) { throw "单元测试未通过" }
}

Write-Host "=== 4/5 PyInstaller 打包（onedir，模型不随包、首启下载） ==="
# PyInstaller 的 COLLECT 阶段会先删除旧 dist\sayo 再写入。在某些受限/受管环境里
# 「一次性删除上百个文件」会被安全策略拦下，此时它会静默放弃写入、继续用旧目录打包，
# 结果是安装包内容与上一次构建完全一致（哈希都不变）却看不出报错。
# 用重命名让步：重命名不是删除，旧目录挪到临时目录交给系统清理。
$OUT = Join-Path $ROOT "dist\sayo"
if (Test-Path $OUT) {
  $stale = Join-Path $env:TEMP ("sayo-stale-" + (Get-Date -Format "yyyyMMdd-HHmmss"))
  Move-Item -LiteralPath $OUT -Destination $stale
  Write-Host "旧产物已挪到 $stale"
}
& $PY -m PyInstaller --noconfirm --distpath "$ROOT\dist" --workpath "$ROOT\build" packaging\sayo.spec
if ($LASTEXITCODE -ne 0) { throw "PyInstaller 打包失败（退出码 $LASTEXITCODE）" }
if (-not (Test-Path (Join-Path $OUT "sayo.exe"))) { throw "打包失败：未生成 sayo.exe" }

Write-Host "=== 5/5 生成离线 ZIP / 可选安装器 ==="
$ZIP = Join-Path $ROOT "dist\sayo-$VERSION-windows-x64.zip"
if (Test-Path $ZIP) { Remove-Item $ZIP -Force }
Compress-Archive -Path $OUT -DestinationPath $ZIP -CompressionLevel Optimal

# 若构建机装有 Inno Setup，可额外生成 setup.exe（同样完全离线）
$ISCC = "C:\Program Files (x86)\Inno Setup 6\ISCC.exe"
if (-not (Test-Path $ISCC)) { $ISCC = "C:\Program Files\Inno Setup 6\ISCC.exe" }
if (Test-Path $ISCC) {
  & $ISCC "/DMyAppVersion=$VERSION" "packaging\windows.iss"
  if ($LASTEXITCODE -ne 0) { throw "Inno Setup 编译失败（退出码 $LASTEXITCODE）" }
  Write-Host "已生成 Inno Setup 安装器"
} else {
  Write-Host "未检测到 Inno Setup，已提供免安装 ZIP（解压即用，长期离线）"
}
Write-Host "完成：$ZIP"
