# 从 Windows 构建机安全发布：只推独立分支，绝不改 main（Mac 的代码和 tag 保持原样）。
#
# 用法（在项目根目录的 PowerShell 里）：
#   powershell -ExecutionPolicy Bypass -File scripts\publish_windows.ps1
#   powershell -ExecutionPolicy Bypass -File scripts\publish_windows.ps1 -DryRun
#
# 做的四件事：
#   1. 取回 GitHub 上的 main（Mac 主线），以它为基线新建分支 feat/windows-support
#   2. 把当前工作区的 Windows 适配改动搬到这个分支上并提交
#   3. 推分支到 GitHub（普通 push，不加 --force）
#   4. 打印把 dist 里的安装包挂到 Release 的具体命令
param(
    [string]$Branch = "feat/windows-support",
    [string]$Message = "feat(windows): Windows 平台适配、构建脚本与安装包支持",
    [string]$Remote = "origin",
    [string]$UserName = "hlc-369123",
    [string]$UserEmail = "hlc-369123@users.noreply.github.com",
    [switch]$DryRun
)

# 用 Continue 而不是 Stop：git 的进度信息走 stderr，在部分终端里会被 PowerShell
# 当成 NativeCommandError 而中断脚本。每一步的关键退出码下面都用 Assert-Ok 单独判断，
# 所以这里的宽松不会掩盖真正的失败。
$ErrorActionPreference = "Continue"
# 让 git 输出的中文按 UTF-8 显示，否则控制台用 GBK 解码会显示成乱码（数据本身没问题）
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}
$ROOT = Split-Path -Parent $PSScriptRoot
Set-Location $ROOT

function Assert-Ok([string]$What) {
    if ($LASTEXITCODE -ne 0) { throw "失败：$What（退出码 $LASTEXITCODE）" }
}

# PowerShell 5.1 把参数按 ANSI 代码页传给 exe，中文提交信息会变乱码；
# 走 UTF-8 临时文件（无 BOM）给 git commit -F 才能保住中文。
function Commit-With-Message([string]$Text) {
    $tmp = Join-Path ([System.IO.Path]::GetTempPath()) ("sayo-commit-" + [guid]::NewGuid().ToString("N") + ".txt")
    [System.IO.File]::WriteAllText($tmp, $Text, (New-Object System.Text.UTF8Encoding($false)))
    git commit -F $tmp | Write-Host
    $code = $LASTEXITCODE
    Remove-Item $tmp -Force -ErrorAction SilentlyContinue
    return $code
}

Write-Host "=== 1/4 检查 ==="
if (-not (Test-Path (Join-Path $ROOT ".git"))) { throw "这里不是 git 仓库：$ROOT" }
if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    throw "找不到 git 命令。请安装 Git for Windows（https://git-scm.com/download/win）后重开终端。"
}
if (-not (git config user.name))  { git config user.name $UserName;  Assert-Ok "设置 user.name" }
if (-not (git config user.email)) { git config user.email $UserEmail; Assert-Ok "设置 user.email" }
Write-Host "目录    : $ROOT"
Write-Host "远程    : $(git remote get-url $Remote)"
Write-Host "目标分支: $Branch"

Write-Host "=== 2/4 取回 main 作为基线 ==="
git fetch $Remote main
if ($LASTEXITCODE -ne 0) {
    throw @"
无法从 GitHub 取回 main。先确认这个网络能打开 https://github.com/hlc-369123/sayo
（常见原因：公司网络拦截 443、代理没生效、需要 VPN）。网络通了重跑同一条命令即可。
"@
}
$base = (git rev-parse "$Remote/main").Trim()
Assert-Ok "解析 $Remote/main"
Write-Host "main 基线: $base"

$dirty = (git status --porcelain | Measure-Object -Line).Lines
Write-Host "待提交改动: $dirty 项"

if ($DryRun) {
    Write-Host "[DryRun] 到此为止，未提交、未推送。改动清单："
    git status --short
    exit 0
}

Write-Host "=== 3/4 把 Windows 改动落到独立分支 ==="
$stashed = $false
if ($dirty -gt 0) {
    git stash push -u -m "sayo-windows-wip"
    Assert-Ok "暂存当前改动"
    $stashed = $true
}

$branchExists = $false
git show-ref --verify --quiet "refs/heads/$Branch"
if ($LASTEXITCODE -eq 0) { $branchExists = $true }

if ($branchExists) {
    git checkout $Branch
    Assert-Ok "切换到 $Branch"
    git merge --ff-only "$Remote/main"
    if ($LASTEXITCODE -ne 0) { Write-Host "分支与 main 已分叉，保持现状继续（不改写历史）" }
} else {
    git checkout -b $Branch $base
    Assert-Ok "新建分支 $Branch"
}

if ($stashed) {
    git stash pop
    if ($LASTEXITCODE -ne 0) {
        throw @"
把 Windows 改动搬到分支时发生冲突（Mac 主线也改过同一个文件）。
改动还在 stash 里，没有丢。处理办法：
  1) git status 看冲突文件，手动保留两边内容
  2) git add <文件> 后执行 git stash drop
  3) 再重跑本脚本（或手动 git commit 然后 git push -u $Remote $Branch）
"@
    }
}

git add -A
Assert-Ok "暂存改动"
$staged = (git diff --cached --name-only | Measure-Object -Line).Lines
if ($staged -gt 0) {
    $code = Commit-With-Message $Message
    if ($code -ne 0) { throw "提交失败（退出码 $code）" }
} else {
    Write-Host "没有新改动需要提交"
}

Write-Host "=== 4/4 推送分支（普通 push，不覆盖 main）==="
git push -u $Remote $Branch
Assert-Ok "推送 $Branch"

# 本地 main 原本是个和 GitHub 无共同历史的孤立快照，
# 推送成功后把它对齐到远程 main，避免以后手滑 git push origin main 把 Mac 历史覆盖掉
if ($Branch -ne "main") {
    git branch -f main "$Remote/main"
    if ($LASTEXITCODE -eq 0) { Write-Host "本地 main 已对齐到 $Remote/main" }
}

Write-Host ""
Write-Host "代码已推送到分支：$Branch"
Write-Host "main 未改动：Mac 用户拉 main、下载现有 Release 都不受影响。"
Write-Host ""
Write-Host "下一步：把安装包挂到 Release（不进 git，46 MB 二进制入库会拖慢每次 clone）"
$exe = Get-ChildItem (Join-Path $ROOT "dist") -Filter "sayo-*-windows-setup.exe" -ErrorAction SilentlyContinue |
    Sort-Object LastWriteTime -Descending | Select-Object -First 1
if ($exe) {
    Write-Host ("本机安装包: {0}  ({1} MB)" -f $exe.FullName, [math]::Round($exe.Length / 1MB, 1))
    Write-Host ""
    Write-Host "方式 A（网页，最稳）: https://github.com/hlc-369123/sayo/releases/new"
    Write-Host "  Tag 填 v1.0.1-windows，Target 选 $Branch，把上面的 exe 拖进附件区"
    Write-Host "方式 B（装了 gh CLI）:"
    Write-Host ('  gh release create v1.0.1-windows "{0}" --target {1} --title "轻述 sayo v1.0.1（Windows）" --prerelease' -f $exe.FullName, $Branch)
} else {
    Write-Host "未在 dist 找到 sayo-*-windows-setup.exe，请先运行 scripts\build_windows.ps1"
}
