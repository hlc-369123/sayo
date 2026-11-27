#!/usr/bin/env python3
"""把本地 Windows 改动提交到 GitHub，并把安装包挂到 Release —— 全程走 REST API。

为什么不用 git push：这台机器访问 github.com:443 被拦（git 协议走不通），
但 api.github.com / uploads.github.com 是通的。所以直接用 API 造一个提交，
父提交是远程 main，效果等价于「在 main 之上开分支并 push」。

用法：
    python scripts/publish_api.py                      # 只预览，什么都不写
    python scripts/publish_api.py --token <PAT>        # 提交源码到分支 + 上传 dist 里的安装包
    python scripts/publish_api.py --token <PAT> --yes-merge   # 再顺便合并进 main

PAT 只需 classic token 勾 repo（要触发 CI 再勾 workflow）。
也可以放在环境变量 GITHUB_TOKEN 里，避免进命令历史。
"""
from __future__ import annotations

import argparse
import base64
import fnmatch
import hashlib
import json
import os
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

API = "https://api.github.com"
UPLOADS = "https://uploads.github.com"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SKIP_DIRS = {".git", ".venv", "venv", "dist", "build", "__pycache__",
             ".workbuddy", ".pytest_cache", ".idea", ".vscode", ".mypy_cache"}


# ---------------------------------------------------------------- HTTP
def request(method: str, url: str, token: str, payload=None, raw: bytes | None = None,
            content_type="application/json", timeout=120):
    data = raw if raw is not None else (json.dumps(payload).encode() if payload is not None else None)
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("User-Agent", "sayo-publisher")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    if data is not None:
        req.add_header("Content-Type", content_type)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read()
            return r.status, (json.loads(body) if body and body[:1] in b"{[" else body)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        try:
            body = json.loads(body)
        except Exception:
            pass
        return e.code, body


_TOKEN = ""


def get_public(url: str):
    """只读接口。不给 token 时会撞上未认证配额（60 次/小时），
    批量推几次就 403；有 token 时一律带上，配额 5000 次/小时。"""
    req = urllib.request.Request(url)
    req.add_header("User-Agent", "sayo-publisher")
    req.add_header("Accept", "application/vnd.github+json")
    if _TOKEN:
        req.add_header("Authorization", f"Bearer {_TOKEN}")
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())


def die(msg: str, code=1):
    print(f"\n✗ {msg}")
    sys.exit(code)


# ---------------------------------------------------------------- 本地文件
def blob_sha(data: bytes) -> str:
    h = hashlib.sha1()
    h.update(b"blob %d\0" % len(data))
    h.update(data)
    return h.hexdigest()


def ignored(paths: list[str]) -> set[str]:
    """用仓库自己的 .gitignore 过滤（模型权重、dist、.venv 等不该入库）。

    用 -z 走 NUL 分隔：Windows 上 text 模式会把 \\n 转成 \\r\\n，
    git 会把 \\r 当成路径的一部分，忽略规则就匹配不上了。
    """
    if not paths:
        return set()
    data = ("\0".join(paths) + "\0").encode("utf-8")
    try:
        p = subprocess.run(["git", "check-ignore", "-z", "--stdin"], cwd=ROOT,
                           input=data, capture_output=True)
    except FileNotFoundError:
        return set()
    return {s.decode("utf-8", "replace") for s in p.stdout.split(b"\0") if s}


def local_files() -> dict[str, str]:
    found: dict[str, str] = {}
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for fn in filenames:
            full = os.path.join(dirpath, fn)
            rel = os.path.relpath(full, ROOT).replace("\\", "/")
            found[rel] = full
    skip = ignored(sorted(found))
    return {rel: full for rel, full in found.items() if rel not in skip}


def diff_against(remote: dict[str, dict], local: dict[str, str]):
    changed, added = [], []
    for rel, full in local.items():
        sha = blob_sha(open(full, "rb").read())
        if rel not in remote:
            added.append(rel)
        elif remote[rel]["sha"] != sha:
            changed.append(rel)
    return sorted(changed), sorted(added)


def wanted_mode(rel: str, remote: dict[str, dict]) -> str:
    """决定 tree entry 的 mode。

    Windows 上 os.access(X_OK) 恒为真，不能据此判断；所以：
      · 远程已存在的文件 → 沿用远程 mode，绝不因为一次重传就把可执行位改掉
      · 新增的脚本 → 按扩展名给 100755
    """
    if rel in remote:
        return remote[rel]["mode"]
    return "100755" if rel.endswith((".sh", ".command")) else "100644"


def real_deletions(remote: dict[str, dict], local: dict[str, str]) -> list[str]:
    """远程有、本地没有的路径里，哪些是「真的被删了」。

    只把**没被 .gitignore 忽略**的路径算作删除：本地 walk 会跳过忽略文件，
    若直接按「本地没有 = 删除」处理，远程一旦存在命中断言忽略规则的文件
    （例如 models/**、*.log），一次推送就会把它从仓库里删掉。
    """
    cand = [p for p in remote if p not in local]
    if not cand:
        return []
    skip = ignored(sorted(cand))
    return sorted(p for p in cand if p not in skip)


# ---------------------------------------------------------------- 主流程
def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _post_asset(args, rel, name: str, path: str, timeout=900) -> bool:
    if name in {a["name"] for a in rel.get("assets", [])}:
        print(f"· Release 里已经有 {name}，跳过上传（要替换请先在网页上删除）")
        return False
    with open(path, "rb") as f:
        payload = f.read()
    print(f"↑ 上传 {name}（{len(payload)/1048576:.1f} MB）…")
    status, res = request(
        "POST",
        f"{UPLOADS}/repos/{args.repo}/releases/{rel['id']}/assets?name={urllib.parse.quote(name)}",
        args.token, raw=payload, content_type="application/octet-stream", timeout=timeout)
    if status not in (200, 201):
        die(f"上传 {name} 失败：{status} {res}")
    print(f"✓ 已上传：{res.get('browser_download_url', '')}")
    return True


def upload_asset(args, rel, tag: str, asset: str, with_sha256=True):
    """上传安装包；同时把 <文件名>.sha256 一起传上去（与 macOS 资产保持一致）。

    没有 .sha256 时，安装脚本的校验分支会被跳过 —— 校验形同虚设，所以这里必传。
    """
    name = os.path.basename(asset)
    _post_asset(args, rel, name, asset)
    if not with_sha256:
        return
    sum_path = asset + ".sha256"
    with open(sum_path, "w", encoding="ascii", newline="\n") as f:
        f.write(f"{sha256_file(asset)}  {name}\n")
    _post_asset(args, rel, name + ".sha256", sum_path, timeout=120)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default="hlc-369123/sayo")
    ap.add_argument("--base", default="main", help="以哪个分支为基线")
    ap.add_argument("--branch", default="feat/windows-support")
    ap.add_argument("--message", default="feat(windows): Windows 平台适配、构建脚本与安装包支持")
    ap.add_argument("--asset", default="", help="要上传的安装包路径，留空自动找 dist 里的 windows setup.exe")
    ap.add_argument("--release-tag", default="", help="挂到哪个 Release 标签，默认最新那个")
    ap.add_argument("--create-release", action="store_true", help="标签不存在时新建 Release")
    ap.add_argument("--prerelease", action="store_true", default=False,
                    help="新建 Release 时标为预发布（不会进「最新正式版」，安装脚本也就找不到它）")
    ap.add_argument("--yes-merge", action="store_true", help="提交后直接合并进 base 分支")
    ap.add_argument("--force-ref", action="store_true",
                    help="分支不是 base 的后代时强制覆盖（基线已前进、需要把分支重建在新基线上时用）")
    ap.add_argument("--token", default=os.environ.get("GITHUB_TOKEN", ""))
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    global _TOKEN
    _TOKEN = args.token

    # 1. 远程基线
    try:
        ref = get_public(f"{API}/repos/{args.repo}/git/ref/heads/{args.base}")
        base_sha = ref["object"]["sha"]
        commit = get_public(f"{API}/repos/{args.repo}/git/commits/{base_sha}")
        base_tree = commit["tree"]["sha"]
        tree = get_public(f"{API}/repos/{args.repo}/git/trees/{base_tree}?recursive=1")
    except Exception as e:
        die(f"读取远程 {args.base} 失败：{e}\n（这台机器需要能访问 api.github.com）")
    remote = {e["path"]: {"sha": e["sha"], "mode": e.get("mode", "100644")}
              for e in tree["tree"] if e["type"] == "blob"}
    print(f"基线 {args.base}@{base_sha[:8]}，远程文件 {len(remote)} 个")

    # 2. 本地差异
    local = local_files()
    changed, added = diff_against(remote, local)
    deleted = real_deletions(remote, local)
    skipped = sorted(p for p in remote if p not in local and p not in deleted)
    print(f"本地文件 {len(local)} 个 → 改动 {len(changed)}，新增 {len(added)}，删除 {len(deleted)}")
    for p in changed:
        print("  M", p)
    for p in added:
        print("  +", p)
    for p in deleted:
        print("  -", p)
    if skipped:
        print(f"  · 另有 {len(skipped)} 个本地被 .gitignore 忽略的远程文件，保持不动")

    # 3. 安装包
    asset = args.asset
    if not asset:
        dist = os.path.join(ROOT, "dist")
        cands = sorted(
            (os.path.join(dist, f) for f in os.listdir(dist) if fnmatch.fnmatch(f, "sayo-*-windows-setup.exe")),
            key=os.path.getmtime, reverse=True) if os.path.isdir(dist) else []
        asset = cands[0] if cands else ""
    if asset and os.path.exists(asset):
        print(f"安装包：{asset}（{os.path.getsize(asset)/1048576:.1f} MB）")
        print(f"  sha256：{sha256_file(asset)}")
    else:
        print("安装包：未找到，本次只提交源码")
        asset = ""

    if args.dry_run or not args.token:
        print("\n预览模式：没有写入任何东西。")
        if not args.token:
            print("加上 --token <PAT> 或设置环境变量 GITHUB_TOKEN 后重跑即可真正提交。")
        return

    # 4. 造提交（等价于在 base 之上开分支 push）
    new_tree_entries = []
    for rel in changed + added:
        data = open(local[rel], "rb").read()
        status, res = request("POST", f"{API}/repos/{args.repo}/git/blobs", args.token,
                              {"content": base64.b64encode(data).decode(), "encoding": "base64"})
        if status not in (200, 201):
            die(f"上传 blob 失败（{rel}）：{status} {res}")
        new_tree_entries.append({"path": rel, "mode": wanted_mode(rel, remote),
                                 "type": "blob", "sha": res["sha"]})
    for rel in deleted:
        new_tree_entries.append({"path": rel, "mode": wanted_mode(rel, remote),
                                 "type": "blob", "sha": None})

    if not new_tree_entries:
        print("\n没有差异，无需提交。")
    else:
        status, res = request("POST", f"{API}/repos/{args.repo}/git/trees", args.token,
                              {"base_tree": base_tree, "tree": new_tree_entries})
        if status not in (200, 201):
            die(f"建 tree 失败：{status} {res}")
        new_tree = res["sha"]

        status, res = request("POST", f"{API}/repos/{args.repo}/git/commits", args.token,
                              {"message": args.message, "tree": new_tree, "parents": [base_sha]})
        if status not in (200, 201):
            die(f"建提交失败：{status} {res}")
        new_commit = res["sha"]
        print(f"\n✓ 提交 {new_commit[:8]}：{args.message}")

        status, _ = request("GET", f"{API}/repos/{args.repo}/git/ref/heads/{args.branch}", args.token)
        if status == 200:
            status, res = request("PATCH", f"{API}/repos/{args.repo}/git/refs/heads/{args.branch}",
                                  args.token, {"sha": new_commit, "force": bool(args.force_ref)})
            if status == 422 and not args.force_ref:
                die(f"分支 {args.branch} 不是 {args.base} 的后代，无法快进。\n"
                    f"确认要把分支重建在新基线上时，加 --force-ref（PR 会保留，只是提交历史换成新的）。")
        else:
            status, res = request("POST", f"{API}/repos/{args.repo}/git/refs", args.token,
                                  {"ref": f"refs/heads/{args.branch}", "sha": new_commit})
        if status not in (200, 201):
            die(f"更新分支 {args.branch} 失败：{status} {res}")
        print(f"✓ 分支 {args.branch} → {new_commit[:8]}（{args.base} 未改动）")

    # 5. 挂安装包（连同 .sha256，与 macOS 资产保持一致）
    if asset:
        tag = args.release_tag
        if not tag:
            rels = get_public(f"{API}/repos/{args.repo}/releases")
            if not rels:
                die("仓库里还没有任何 Release，请用 --release-tag 指定并加 --create-release")
            tag = rels[0]["tag_name"]

        status, rel = request("GET", f"{API}/repos/{args.repo}/releases/tags/{tag}", args.token)
        if status == 404:
            if not args.create_release:
                die(f"Release {tag} 不存在。加 --create-release 新建，或换一个 --release-tag。")
            print(f"！注意：新建的 Release 只有 Windows 资产。macOS 用户的安装脚本按「最新正式版」查找，"
                  f"\n  若该 Release 设为正式版，他们会取到这一页却找不到 dmg。推荐做法是把 Windows 包"
                  f"\n  挂到已有版本页（与 mac 资产并列），不加 --create-release。")
            status, rel = request("POST", f"{API}/repos/{args.repo}/releases", args.token, {
                "tag_name": tag, "target_commitish": args.branch,
                "name": f"轻述 sayo {tag}（Windows）", "prerelease": args.prerelease,
                "body": "Windows 安装包。安装包不含语音识别模型：安装完成后应用会在后台自动下载一次"
                        "（约 240 MB，校验 SHA-256，成功后不再联网）。",
            })
            if status not in (200, 201):
                die(f"新建 Release 失败：{status} {rel}")
        elif status != 200:
            die(f"读取 Release {tag} 失败：{status} {rel}")

        upload_asset(args, rel, tag, asset, with_sha256=True)

    # 6. 合并
    if args.yes_merge:
        status, res = request("POST", f"{API}/repos/{args.repo}/merges", args.token,
                              {"base": args.base, "head": args.branch,
                               "commit_message": f"Merge {args.branch} into {args.base}"})
        if status == 204:
            print(f"\n· {args.base} 已经包含 {args.branch}，无需合并")
        elif status in (200, 201):
            print(f"\n✓ 已合并进 {args.base}：{res['sha'][:8]}")
        else:
            die(f"合并失败：{status} {res}")

    print("\n完成。打开 https://github.com/%s 查看。" % args.repo)


if __name__ == "__main__":
    main()
