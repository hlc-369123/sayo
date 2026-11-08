#!/usr/bin/env bash
# 轻述 sayo：一键诊断 GitHub 推送通道，自动挑可用通道，然后推送 main 与标签。
#
# 在「终端.app」里执行（不要用 IDE 内置终端）：
#     cd "/Users/hlc/Documents/语言转文字输入" && bash scripts/push_github.sh
# 只想诊断、不推送：
#     bash scripts/push_github.sh --probe-only
# 网络不稳、整包总被掐断时，直接多轮分批推送（推荐）：
#     bash scripts/push_github.sh --chunked-only
# 中断了直接重跑同一条命令即可，会从远程已有的提交继续。
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
MODE="${1:-}"

COMMITS_FILE="$(mktemp -t sayo-commits)"
PUSH_ERR="$(mktemp -t sayo-push-err)"
# 本地断点：网络抖动导致 git ls-remote 查不到远程 HEAD 时，仍能接着上次继续
PROGRESS="$(git rev-parse --git-dir)/sayo-push-progress"
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
  if [ "${rc}" -eq 0 ]; then
    echo "OK  $url"
    return 0
  fi
  echo "--  $url 这次没通（rc=${rc}）"
  printf '%s\n' "$out" | head -5 | sed 's/^/    /'
  return 1
}

CHANNEL="$SSH22"
if [ "$MODE" = "--chunked-only" ]; then
  echo "──────── 2/4 快速模式：跳过探测，直接分批推送 ────────"
else
  echo "──────── 2/4 探测通道（仅用于选路，探测不通也照样尝试推送） ────────"
  for url in "$SSH22" "$SSH443"; do
    for attempt in 1 2; do
      if probe "$url"; then
        CHANNEL="$url"
        break 2
      fi
      [ "${attempt}" -lt 2 ] && echo "   第 ${attempt} 次未通，重试…"
    done
  done
fi
# 两条探测都不通也不放弃：仍以 22 为准做一次真实 push，由真实结果说话
echo "选定通道: $CHANNEL"

if [ "$MODE" = "--probe-only" ]; then
  echo "诊断完成（未推送）"
  exit 0
fi

# 按提交顺序逐个 fast-forward，把一次大上传拆成多次小上传。
# 天然幂等：已在远程历史里的提交会被跳过，中途断了重跑可从断点继续。
chunked_push() {
  local remote_head done_sha c ok skip a label
  done_sha="$(cat "$PROGRESS" 2>/dev/null || true)"
  remote_head=""
  for a in 1 2 3; do
    remote_head="$(git ls-remote --heads origin main 2>/dev/null | awk '{print $1}')"
    [ -n "$remote_head" ] && break
  done
  git rev-list --reverse main > "$COMMITS_FILE"
  while read -r c; do
    # 本地进度 + 远程 HEAD 双重跳过，任一即可
    if [ -n "$done_sha" ] && git merge-base --is-ancestor "$c" "$done_sha" 2>/dev/null; then
      continue
    fi
    if [ -n "$remote_head" ] && git merge-base --is-ancestor "$c" "$remote_head" 2>/dev/null; then
      printf '%s\n' "$c" > "$PROGRESS"
      done_sha="$c"
      continue
    fi
    ok=0
    skip=0
    for a in 1 2 3; do
      if git push --quiet origin "${c}:refs/heads/main" 2>"$PUSH_ERR"; then
        ok=1
        break
      fi
      # 远程已包含该提交（多因上一次查询失败），直接跳过，不要死重试
      if grep -qE 'non-fast-forward|rejected' "$PUSH_ERR"; then
        skip=1
        break
      fi
    done
    label="$(git show -s --format='%h %s' "$c" | cut -c1-56)"
    if [ "${ok}" = 1 ] || [ "${skip}" = 1 ]; then
      if [ "${skip}" = 1 ]; then
        echo "  SKIP $label（远程已包含）"
      else
        echo "  OK   $label"
      fi
      printf '%s\n' "$c" > "$PROGRESS"
      done_sha="$c"
    else
      echo "  FAIL $label"
      head -3 "$PUSH_ERR" | sed 's/^/       /'
      return 1
    fi
  done < "$COMMITS_FILE"
  return 0
}

echo "──────── 3/4 推送 ────────"
git remote set-url origin "$CHANNEL"
echo "远程已设为: $CHANNEL"

PUSHED=0
if [ "$MODE" != "--chunked-only" ]; then
  for a in 1 2; do
    echo "── 整包推送尝试 ${a}/2 ──"
    if git push -u origin main; then
      PUSHED=1
      break
    fi
  done
fi

if [ "${PUSHED}" = 0 ]; then
  echo ""
  echo "改用分批推送（逐提交小上传，断了重跑会从断点继续）…"
  for round in 1 2 3 4 5 6; do
    if chunked_push; then
      PUSHED=1
      break
    fi
    echo "  ── 第 ${round} 轮中断，从断点继续 ──"
  done
  if [ "${PUSHED}" = 1 ]; then
    git push -u --quiet origin main || true
  fi
fi

if [ "${PUSHED}" = 0 ]; then
  echo ""
  echo "── 补充判断：SSH 认证本身是否正常 ──"
  auth="$(ssh -o BatchMode=yes -o ConnectTimeout=15 -T git@github.com 2>&1)"
  printf '%s\n' "$auth" | head -4 | sed 's/^/    /'
  if printf '%s' "$auth" | grep -q "successfully authenticated"; then
    echo "   ↑ 这是【认证成功】，密钥和账号都没问题，是网络在传数据时掐断了连接。"
  fi
  echo "   远程已有若干提交，重跑本脚本会从断点继续；也可关闭代理 / VPN、换手机热点。"
  echo ""
  echo "❌ 本次未全部推完。兜底方案：HTTPS + Personal Access Token"
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
echo "远程 main: $(git ls-remote --heads origin main 2>/dev/null | awk '{print $1}')"
echo "本地 main: $(git rev-parse main)"
