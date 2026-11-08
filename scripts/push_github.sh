#!/usr/bin/env bash
# 轻述 sayo：一键诊断 GitHub 推送权限，自动挑可用通道，然后推送 main 与标签。
#
# 在「终端.app」里执行（不要用 IDE 内置终端）：
#     cd "/Users/hlc/Documents/语言转文字输入" && bash scripts/push_github.sh
# 只想诊断、不推送：
#     bash scripts/push_github.sh --probe-only
#
# 说明：本机 ~/.ssh/id_ed25519.pub 已登记在 GitHub 账号 hlc-369123 上，
# 所以「没有正确权限」通常是 22 端口被网络拦截，而不是密钥失效。
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

ssh_ok() {
  ssh -o BatchMode=yes -o ConnectTimeout=8 -o StrictHostKeyChecking=accept-new \
      -T -p "$2" "git@$1" 2>&1 | grep -q "successfully authenticated"
}

echo "──────── 2/4 探测 SSH 通道 ────────"
CHANNEL=""
if ssh_ok github.com 22; then
  echo "OK  22 端口可认证"; CHANNEL="$SSH22"
else
  echo "--  22 端口失败，原始报错："
  ssh -o BatchMode=yes -o ConnectTimeout=8 -T git@github.com 2>&1 | head -8 | sed 's/^/    /'
  if ssh_ok ssh.github.com 443; then
    echo "OK  443 端口可认证（走 ssh.github.com）"; CHANNEL="$SSH443"
  else
    echo "--  443 端口失败，原始报错："
    ssh -o BatchMode=yes -o ConnectTimeout=8 -T -p 443 git@ssh.github.com 2>&1 | head -8 | sed 's/^/    /'
  fi
fi

if [ -z "$CHANNEL" ]; then
  echo ""
  echo "❌ 两条 SSH 通道都认证失败。改用 HTTPS + Personal Access Token："
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
  echo "把上面两段「原始报错」一起发回来，可以进一步定位。"
  exit 1
fi

if [ "${1:-}" = "--probe-only" ]; then
  echo "诊断完成，可用通道：$CHANNEL（未推送）"
  exit 0
fi

echo "──────── 3/4 设置远程并推送 ────────"
# 避免首次连接被 known_hosts 交互提示卡住（"Host key verification failed" 也会报成没有权限）
export GIT_SSH_COMMAND="ssh -o StrictHostKeyChecking=accept-new -o ConnectTimeout=15 -o ServerAliveInterval=20"
git remote set-url origin "$CHANNEL"
echo "远程已设为: $CHANNEL"

if ! git push -u origin main; then
  echo ""
  echo "❌ 代码推送失败。若报错是连接被重置 / broken pipe，多为网络对大流量上传不友好，可重试："
  echo "      git push -u origin main"
  echo "   或先单独推一次空提交试探通道，再推全量："
  echo "      git config http.postBuffer 524288000"
  echo "   也可把 dist/sayo-full.bundle（27MB 仓库完整备份）先发过去应急。"
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
