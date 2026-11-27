#!/usr/bin/env bash
# 只下 macOS 该下的那一个安装包：自动读取最新 Release，挑 *macos* 资产。
# 用法：bash scripts/install_sayo.sh            # 下载 DMG 并打开
#      bash scripts/install_sayo.sh --pkg      # 下载 PKG
#      bash scripts/install_sayo.sh --list     # 只看这个版本有哪些文件
set -euo pipefail

REPO="${REPO:-hlc-369123/sayo}"
MODE="dmg"
for a in "$@"; do
  case "$a" in
    --pkg) MODE="pkg" ;;
    --dmg) MODE="dmg" ;;
    --list) MODE="list" ;;
    *) echo "未知参数：$a" >&2; exit 1 ;;
  esac
done

# 不读 /releases/latest（那是「最新正式版」，Windows 包若挂在预发布页就会漏掉）；
# 改成从新到旧扫，取第一个真的含本平台安装包的发布。
API="https://api.github.com/repos/$REPO/releases?per_page=30"
TMP="$(mktemp -t sayo-rel)"
trap 'rm -f "$TMP"' EXIT

curl -fsSL -H "User-Agent: sayo-installer" "$API" -o "$TMP"

PICK="$(python3 - "$TMP" "$MODE" <<'PY'
import json, sys
data = json.load(open(sys.argv[1]))
mode = sys.argv[2]
rels = [r for r in data if not r.get("draft")] if isinstance(data, list) else []
if not rels:
    print("ERROR=没有可用的发布")
    sys.exit(0)

if mode == "list":
    r = rels[0]
    print("TAG=" + r.get("tag_name", ""))
    for a in r.get("assets", []):
        print("ASSET=%s\t%d" % (a["name"], a["size"]))
    sys.exit(0)

want = "macos.%s" % mode
for r in rels:
    assets = r.get("assets", [])
    chosen = [a for a in assets if a["name"].endswith(want)]
    if chosen:
        a = chosen[0]
        print("TAG=" + r.get("tag_name", ""))
        print("NAME=" + a["name"])
        print("URL=" + a["browser_download_url"])
        print("SHAURL=" + next((x["browser_download_url"] for x in assets
                                if x["name"] == a["name"] + ".sha256"), ""))
        sys.exit(0)

newest = rels[0]
print("ERROR=最近 30 个发布里都没有 %s 文件；最新一版只有：%s" % (
    want, ", ".join(a["name"] for a in newest.get("assets", []))))
PY
)"

if grep -q '^ERROR=' <<<"$PICK"; then
  sed -n 's/^ERROR=//p' <<<"$PICK" >&2
  exit 1
fi

if [ "$MODE" = "list" ]; then
  sed -n 's/^TAG=/版本：/p' <<<"$PICK"
  sed -n 's/^ASSET=/\  - /p' <<<"$PICK" | awk -F'\t' '{printf "%s (%.1f MB)\n", $1, $2/1048576}'
  exit 0
fi

NAME="$(sed -n 's/^NAME=//p' <<<"$PICK")"
URL="$(sed -n 's/^URL=//p' <<<"$PICK")"
SHAURL="$(sed -n 's/^SHAURL=//p' <<<"$PICK")"
TAG="$(sed -n 's/^TAG=//p' <<<"$PICK")"
DEST="${TMPDIR:-/tmp}/$NAME"

echo "版本 $TAG，下载 $NAME …"
curl -fsSL -H "User-Agent: sayo-installer" "$URL" -o "$DEST"

if [ -n "$SHAURL" ]; then
  curl -fsSL -H "User-Agent: sayo-installer" "$SHAURL" -o "$DEST.sha256"
  (cd "$(dirname "$DEST")" && shasum -a 256 -c "$(basename "$DEST").sha256")
else
  echo "⚠️  这个版本没有提供 $NAME.sha256，已跳过完整性校验；请确认下载来源可信。" >&2
fi

echo "下载完成：$DEST"
open "$DEST"
