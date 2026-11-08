#!/usr/bin/env bash
# 轻述 sayo：一键诊断 GitHub 推送通道，自动挑可用通道，然后推送 main 与标签。
#
# 在「终端.app」里执行（不要用 IDE 内置终端）：
#     cd "/Users/hlc/Documents/语言转文字输入" && bash scripts/push_github.sh
# 只想诊断、不推送：
#     bash scripts/push_github.sh --probe-only
#
# 注意：GitHub 对 `ssh -T git@github.com` 永远返回非零退出码，
# 并把 "You've successfully authenticated" 当作**成功消息**打印。
# 不能用 ssh -T 的退出码判断权限，必须用真实 git 协议（ls-remote）判断。
set -u

cd "$(dirname "$0")/.." || { echo "找不到项目目录"; exit 1; }

SSH22="git@github.com:hlc-369123/sayo.git"
SSH443="ssh://git@ssh.github.com:443/hlc-369123/sayo.git"
HTTPS_URL="https://github.com/hlc-369123/sayo.git"

echo "──────── 1/4 本机与仓库信息 ────────"
git --version
echo "当前目录 : $(pwd)"
echo "当前提交 : $(git log -1 --format='%h %s')"
echo "未提交   : $(git status --porcelain | wc -l | tr -d ' ') 个改动"
echo "当前远程 : $(git remote get-url origin)"
if [ -f "$HOME/.ssh/id_ed25519.pub" ]; then
  echo "公钥指纹 : $(ssh-keygen -lf "$HOME/.ssh/id_ed25519.pub")"
else
  echo "公钥指纹 : 未找到 ~/.ssh/id_ed25519.pub"
fi

# 长一点的连接超时 + 保活，避免代理偶发掐断被误判成「认证失败」
export GIT_SSH_COMMAND="ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new -o ConnectTimeout=20 -o ServerAliveInterval=15 -o ServerAliveCountMax=4"

# 用真实 git 协议探测：空仓库返回 0 且无输出；认证/连接失败返回非 0
probe() {
  local url="$1" out rc
  out="$(git ls-remote "$url" 2>&1)"
  rc=$?
  if [ "$rc" -eq 0 ]; then
    echo "OK  $url"
    return 0
  fi
  echo "--  $url 不通（rc=$rc），原始报错："
  printf '%s\n' "$out" | head -8 | sed 's/^/    /'
  return 1
}

echo "──────── 2/4 探测可用通道（每条最多 2 次，代理偶发掐断会自动重试） ────────"
CHANNEL=""
for url in "$SSH22" "$SSH443"; do
  for attempt in 1 2; do
    if probe "$url"; then
      CHANNEL="$url"
      break 2
    fi
    [ "$attempt" -lt 2 ] && echo "   第 $attempt 次未通，稍后重试…"
  done
done

if [ -z "$CHANNEL" ]; then
  echo ""
  echo "── 补充判断：SSH 认证本身是否正常 ──"
  auth="$(ssh -o BatchMode=yes -o ConnectTimeout=15 -T git@github.com 2>&1)"
  printf '%s\n' "$auth" | head -4 | sed 's/^/    /'
  if printf '%s' "$auth" | grep -q "successfully authenticated"; then
    echo "   ↑ 这行是【认证成功】，GitHub 不提供 shell 属正常现象。"
    echo "   说明密钥没问题，是代理/网络在 git 数据阶段掐断了连接。"
    echo "   建议：关闭代理或 VPN、换手机热点后重跑本脚本。"
  fi
  echo ""
  echo "❌ 两条 SSH 通道都不可用。兜底方案：HTTPS + Personal Access Token"
  echo "   1) 浏览器打开 https://github.com/settings/tokens/new"
  echo "      选 classic token，勾选 repo，生成后复制（只显示一次）"
  echo "   2) 终端执行："
  echo "      cd \"$(pwd)\""
  echo "      git remote set-url origin $HTTPS_URL"
  echo "      printf 'protocol=https\\nhost=github.com\\n' | git credential-osxkeychain erase"
  echo "      git push -u origin main"
  echo "      # 用户名填 hlc-369123，密码粘贴刚才的 token"
  echo "      git push --tags"
  echo ""
  echo "把上面所有「原始报错」一起发回来，可以进一步定位。"
  exit 1
fi

if [ "${1:-}" = "--probe-only" ]; then
  echo "诊断完成，可用通道：$CHANNEL（未推送）"
  exit 0
fi

echo "──────── 3/4 设置远程并推送 ────────"
git remote set-url origin "$CHANNEL"
echo "远程已设为: $CHANNEL"

if ! git push -u origin main; then
  echo ""
  echo "❌ 代码推送失败（原始报错见上）。通道探测是通的，多为上传阶段被网络掐断，可重试："
  echo "      git push -u origin main"
  echo "   若反复失败，关闭代理 / 换网络后重跑：bash scripts/push_github.sh"
  echo "   应急：dist/sayo-full.bundle 是完整仓库备份，可直接传给对方 clone。"
  exit 1
fi

if ! git push --tags; then
  echo "⚠️ 代码已推送成功，标签推送失败，稍后重试: git push --tags"
  exit 1
fi

echo "──────── 4/4 完成 ────────"
git remote -v
echo "仓库地址: https://github.com/hlc-369123/sayo"
echo "远程分支: $(git ls-remote --heads origin 2>/dev/null | head -5)"
