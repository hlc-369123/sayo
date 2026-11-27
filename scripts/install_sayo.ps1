# 只下 Windows 该下的那一个安装包：自动读取最新 Release，挑 *windows* 资产。
# 用法（在 PowerShell 里）：irm https://raw.githubusercontent.com/hlc-369123/sayo/main/scripts/install_sayo.ps1 | iex
# 或本地：powershell -ExecutionPolicy Bypass -File scripts\install_sayo.ps1
param(
    [string]$Repo = "hlc-369123/sayo",
    [string]$Pattern = "*windows*setup*.exe",
    [string]$OutDir = "$env:TEMP\sayo-download"
)

$ErrorActionPreference = "Stop"
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null

$api = "https://api.github.com/repos/$Repo/releases?per_page=30"
Write-Host "查询发布列表：$api"
# 不读 /releases/latest：那是「最新正式版」，如果 Windows 包挂在预发布页上就会取不到。
# 这里改成从新到旧扫，取第一个**真的含本平台安装包**的发布。
$rels = Invoke-RestMethod -Uri $api -Headers @{ "User-Agent" = "sayo-installer" }
$rel = $null
$asset = $null
foreach ($r in $rels) {
    if ($r.draft) { continue }
    $a = $r.assets | Where-Object { $_.name -like $Pattern } | Select-Object -First 1
    if ($a) { $rel = $r; $asset = $a; break }
}
if (-not $asset) {
    Write-Host "最近 30 个发布里都没有匹配 '$Pattern' 的文件。最新一版现有文件："
    if ($rels.Count -gt 0) { $rels[0].assets | ForEach-Object { Write-Host "  - $($_.name)" } }
    exit 1
}
Write-Host "版本：$($rel.tag_name)"

$dest = Join-Path $OutDir $asset.name
Write-Host "下载 $($asset.name)（$([math]::Round($asset.size / 1MB, 1)) MB）…"
Invoke-WebRequest -Uri $asset.browser_download_url -OutFile $dest -UseBasicParsing

$sum = $rel.assets | Where-Object { $_.name -eq "$($asset.name).sha256" } | Select-Object -First 1
if ($sum) {
    $tmp = "$dest.sha256"
    Invoke-WebRequest -Uri $sum.browser_download_url -OutFile $tmp -UseBasicParsing
    $expect = (Get-Content $tmp -Raw).Split(" ")[0].Trim().ToLower()
    $actual = (Get-FileHash $dest -Algorithm SHA256).Hash.ToLower()
    if ($expect -ne $actual) { throw "校验失败：期望 $expect，实际 $actual" }
    Write-Host "sha256 校验通过"
} else {
    Write-Warning "这个版本没有提供 $($asset.name).sha256，已跳过完整性校验；请确认下载来源可信。"
}

Write-Host "开始安装：$dest"
Start-Process -FilePath $dest -Wait
