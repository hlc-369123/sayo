#!/usr/bin/env bash
# 轻述 sayo：一键诊断 GitHub 推送通道，自动挑可用通道，然后推送 main 与标签。
#
# 在「终端.app」里执行（不要用 IDE 内置终端）：
#     cd "/Users/hlc/Documents/语言转文字输入" && bash scripts/push_github.sh
# 只想诊断、不推送：
#     bash scripts/push_github.sh --probe-only
#
# 两个已踩过的坑：
#  1. GitHub 对 `ssh -T git@github.com` 永远返回非零，并把
#     "You've successfully authenticated" 当作【成功消息】打印。
#     不能用它判断权限；git 的 "Please make sure you have the correct
#     access rights" 也只是连接中断时的兜底文案，同样不代表密钥失效。
#  2. 代理可能在 pack 传到一半时掐断（unexpected disconnect while
#     reading sideband packet）。整包推送失败时自动改为按提交分批推送。
set -u

cd "$(dirname "$0")/.." || { echo "找不到项目目录"; exit 1; }

SSH22="git@github.com:hlc-369123/sayo.git"
SSH443="ssh://git@ssh.github.com:443/hlc-369123/sayo.git"
HTTPS_URL="https://github.com/hlc-369123/sayo.git"

COMMITS_FILE="$(mktemp -t sayo-commits)"
PUSH_ERR="$(mktemp -t sayo-push-err)"
trap 'rm -f "$COMMITS_FILE" "$PUSH_ERR"' EXIT

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

# 长连接超时 + 保活 + SSH 层压缩，减小被代理掐断的概率
export GIT_SSH_COMMAND="ssh -C -o BatchMode=yes -o StrictHostKeyChecking=accept-new -o ConnectTimeout=20 -o ServerAliveInterval=10 -o ServerAliveCountMax=6"

# 真实 git 协议探测：空仓库返回 0 且无输出；连接失败返回非 0
probe() {
  local url="$1" out rc
  out="$(git ls-remote "$url" 2>&1)"
  rc=$?
  if [ "$rc" -eq 0 ]; then
    echo "OK  $url"
    return 0
  fi
  echo "--  $url 这次没通（rc=$rc）："
  printf '%s\n' "$out" | head -5 | sed 's/^/    /'
  return 1
}

echo "──────── 2/4 探测通道（仅用于选路，探测不通也照样尝试推送） ────────"
CHANNEL="$SSH22"
for url in "$SSH22" "$SSH443"; do
  for attempt in 1 2; do
    if probe "$url"; then
      CHANNEL="$url"
      break 2
    fi
    [ "$attempt" -lt 2 ] && echo "   第 $attempt 次未通，重试…"
  done
done
# 两条探测都不通也不放弃：仍以 22 为准做一次真实 push，由真实结果说话
echo "选定通道: $CHANNEL"

if [ "${1:-}" = "--probe-only" ]; then
  echo "诊断完成（未推送）"
  exit 0
fi

# 整包推送失败时，按提交顺序逐个 fast-forward，把一次大上传拆成多次小上传。
# 天然幂等：已在远程历史里的提交会被跳过，中途断了重跑可从断点继续。
chunked_push() {
  local remote_head c ok a label
  remote_head="$(git ls-remote --heads origin main 2>/dev/null | awk '{print $1}')"
  git rev-list --reverse main > "$COMMITS_FILE"
  while read -r c; do
    if [ -n "$remote_head" ] && git merge-base --is-ancestor "$c" "$remote_head" 2>/dev/null; then
      continue
    fi
    ok=0
    for a in 1 2 3; do
      if git push --quiet origin "${c}:refs/heads/main" 2>"$PUSH_ERR"; then
        ok=1
        break
      fi
    done
    label="$(git show -s --format='%h %s' "$c" | cut -c1-56)"
    if [ "$ok" = 1 ]; then
      echo "  OK   $label"
      remote_head="$c"
    else
      echo "  FAIL $label"
      head -4 "$PUSH_ERR" | sed 's/^/       /'
      return 1
    fi
  done < "$COMMITS_FILE"
  return 0
}

echo "──────── 3/4 推送 ────────"
git remote set-url origin "$CHANNEL"
echo "远程已设为: $CHANNEL"

PUSHED=0
for a in 1 2 3; do
  echo "── 整包推送尝试 $a/3 ──"
  if git push -u origin main; then
    PUSHED=1
    break
  fi
done

if [ "$PUSHED" = 0 ]; then
  echo ""
  echo "整包推送未成功，改用分批推送（把仓库拆成逐次小上传）…"
  if chunked_push; then
    git push -u --quiet origin main || true
    PUSHED=1
  fi
fi

if [ "$PUSHED" = 0 ]; then
  echo ""
  echo "── 补充判断：SSH 认证本身是否正常 ──"
  auth="$(ssh -o BatchMode=yes -o ConnectTimeout=15 -T git@github.com 2>&1)"
  printf '%s\n' "$auth" | head -4 | sed 's/^/    /'
  if printf '%s' "$auth" | grep -q "successfully authenticated"; then
    echo "   ↑ 这是【认证成功】，密钥和账号都没问题，是网络在传数据时掐断了连接。"
    echo "   建议：关闭代理 / VPN，或换手机热点后重跑本脚本（会从未完成的提交继续）。"
  fi
  echo ""
  echo "❌ 仍未推上去。兜底方案：HTTPS + Personal Access Token"
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
  echo "把完整输出贴回来，可以进一步定位。"
  exit 1
fi

if ! git push --tags; then
  echo "⚠️ 代码已推送成功，标签推送失败，稍后重试: git push --tags"
fi

echo "──────── 4/4 完成 ────────"
git remote -v
echo "仓库地址: https://github.com/hlc-369123/sayo"
echo "远程分支: $(git ls-remote --heads origin 2>/dev/null | head -5)"
