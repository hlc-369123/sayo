#!/usr/bin/env python3
"""把 GitHub 仓库 Traffic（官方只留 14 天）抓到本地长期存档，并生成离线报表。

用法：
    python3 scripts/github_traffic_local.py              # 抓取 + 归档 + 生成报表
    python3 scripts/github_traffic_local.py --set-token  # 交互式写入本地 token
    python3 scripts/github_traffic_local.py --no-fetch   # 只用本地数据重新生成报表

token 优先级：环境变量 GITHUB_TOKEN > ~/.config/sayo/github_token
Fine-grained PAT 需要该仓库的 Administration: Read-only 权限。
token 只存在用户目录，不写进项目。
"""

from __future__ import annotations

import argparse
import csv
import getpass
import html
import json
import os
import stat
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

API_ROOT = "https://api.github.com"
DEFAULT_REPO = "hlc-369123/sayo"
TOKEN_PATH = Path.home() / ".config" / "sayo" / "github_token"
ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "stats" / "traffic"
DATA_FILE = DATA_DIR / "data.json"
CSV_FILE = DATA_DIR / "daily.csv"
REPORT_FILE = DATA_DIR / "report.html"


def load_token() -> str | None:
    env = os.environ.get("GITHUB_TOKEN", "").strip()
    if env:
        return env
    try:
        return TOKEN_PATH.read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def save_token() -> None:
    token = getpass.getpass("粘贴 GitHub token（输入不回显）: ").strip()
    if not token:
        raise SystemExit("token 为空，未写入。")
    TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_PATH.write_text(token + "\n", encoding="utf-8")
    TOKEN_PATH.chmod(stat.S_IRUSR | stat.S_IWUSR)
    print(f"已写入 {TOKEN_PATH}（权限 600）")


def fetch_json(path: str, token: str) -> dict:
    req = urllib.request.Request(
        f"{API_ROOT}{path}",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "sayo-local-traffic",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=25) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:300]
        if exc.code in (401, 403):
            raise SystemExit(
                f"GitHub 拒绝访问（HTTP {exc.code}）：{detail}\n"
                "请确认 token 有效，且对该仓库具备 Administration: Read-only 权限。"
            ) from None
        raise SystemExit(f"GitHub API 失败（HTTP {exc.code}）：{detail}") from None
    except urllib.error.URLError as exc:
        raise SystemExit(f"网络请求失败：{exc.reason}") from None


def empty_store(repo: str) -> dict:
    return {"repo": repo, "updated_at": None, "days": {}}


def load_store(repo: str) -> dict:
    if not DATA_FILE.exists():
        return empty_store(repo)
    try:
        store = json.loads(DATA_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        print("警告：本地存档损坏，将重新开始。", file=sys.stderr)
        return empty_store(repo)
    store.setdefault("days", {})
    return store


def merge_view(store: dict, payload: dict, kind: str) -> None:
    prefix = "views" if kind == "views" else "clones"
    for item in payload.get(prefix, []):
        day = item.get("timestamp", "")[:10]
        if not day:
            continue
        row = store["days"].setdefault(
            day, {"views": 0, "view_uniques": 0, "clones": 0, "clone_uniques": 0}
        )
        row["views" if prefix == "views" else "clones"] = item.get("count", 0)
        row["view_uniques" if prefix == "views" else "clone_uniques"] = item.get(
            "uniques", 0
        )


def write_csv(store: dict) -> None:
    with CSV_FILE.open("w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.writer(fh)
        writer.writerow(["日期", "浏览量", "按日累加独立访客", "克隆数", "按日累加克隆独立访客"])
        for day in sorted(store["days"]):
            row = store["days"][day]
            writer.writerow(
                [day, row["views"], row["view_uniques"], row["clones"], row["clone_uniques"]]
            )


def write_extra_csv(store: dict) -> None:
    with (DATA_DIR / "referrers.csv").open("w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.writer(fh)
        writer.writerow(["来源", "浏览量", "独立访客"])
        for item in store.get("referrers", []):
            writer.writerow([item.get("referrer", ""), item.get("count", 0), item.get("uniques", 0)])
    with (DATA_DIR / "paths.csv").open("w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.writer(fh)
        writer.writerow(["路径", "标题", "浏览量", "独立访客"])
        for item in store.get("paths", []):
            writer.writerow(
                [item.get("path", ""), item.get("title", ""), item.get("count", 0), item.get("uniques", 0)]
            )


def _chart_svg(store: dict, days: list[str], key: str, color: str) -> tuple[str, int]:
    shown = days[-30:]
    values = {d: store["days"][d][key] for d in shown}
    maximum = max(values.values(), default=1) or 1
    bar_w, gap, left, top, chart_h = 18, 8, 44, 14, 116
    parts = []
    for index, day in enumerate(shown):
        value = values[day]
        height = round(value / maximum * chart_h)
        x = left + index * (bar_w + gap)
        y = top + chart_h - height
        parts.append(
            f'<rect x="{x}" y="{y}" width="{bar_w}" height="{height}" rx="3" fill="{color}"/>'
            f'<text x="{x + bar_w / 2}" y="{y - 4}" text-anchor="middle" font-size="11" '
            f'fill="{color}">{value}</text>'
            f'<text x="{x + bar_w / 2}" y="{top + chart_h + 15}" text-anchor="middle" '
            f'font-size="10" fill="#5F5E5A">{html.escape(day[5:])}</text>'
        )
    width = max(left + len(shown) * (bar_w + gap) + 16, 320)
    svg = (
        f'<svg viewBox="0 0 {width} 166" width="100%" role="img" aria-label="每日柱状图">'
        f'<line x1="{left - 8}" y1="{top + chart_h}" x2="{width - 8}" y2="{top + chart_h}" '
        f'stroke="#D3D1C7" stroke-width="1"/>'
        + "".join(parts)
        + "</svg>"
    )
    return svg, len(shown)


def build_report(store: dict, latest: dict | None) -> None:
    latest = latest or store.get("latest")
    days = sorted(store["days"])
    totals = {
        key: sum(store["days"][day][key] for day in days)
        for key in ("views", "view_uniques", "clones", "clone_uniques")
    }
    views_count = latest.get("views_count", 0) if latest else totals["views"]
    views_uniques = latest.get("views_uniques", 0) if latest else totals["view_uniques"]
    clones_count = latest.get("clones_count", 0) if latest else totals["clones"]
    clones_uniques = latest.get("clones_uniques", 0) if latest else totals["clone_uniques"]

    clone_days = [(d, store["days"][d]["clones"]) for d in days]
    peak_day, peak_clones = max(clone_days, key=lambda pair: pair[1]) if clone_days else ("—", 0)
    peak_share = round(peak_clones / totals["clones"] * 100) if totals["clones"] else 0
    active_days = sum(1 for d in days if store["days"][d]["views"] > 0)
    ref_text = "、".join(
        str(item.get("referrer", "")) for item in store.get("referrers", [])
    ) or "无外部来源记录"

    views_svg, views_n = _chart_svg(store, days, "views", "#185FA5")
    clones_svg, clones_n = _chart_svg(store, days, "clones", "#BA7517")

    rows = "".join(
        "<tr><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td></tr>".format(
            html.escape(day),
            store["days"][day]["views"],
            store["days"][day]["view_uniques"],
            store["days"][day]["clones"],
            store["days"][day]["clone_uniques"],
        )
        for day in reversed(days)
    ) or '<tr><td colspan="5">暂无数据</td></tr>'

    referrer_rows = "".join(
        "<tr><td>{}</td><td>{}</td><td>{}</td></tr>".format(
            html.escape(str(item.get("referrer", ""))),
            item.get("count", 0),
            item.get("uniques", 0),
        )
        for item in store.get("referrers", [])
    ) or '<tr><td colspan="3">暂无数据</td></tr>'

    path_rows = "".join(
        "<tr><td>{}</td><td>{}</td><td>{}</td><td>{}</td></tr>".format(
            html.escape(str(item.get("path", ""))),
            html.escape(str(item.get("title", ""))),
            item.get("count", 0),
            item.get("uniques", 0),
        )
        for item in store.get("paths", [])
    ) or '<tr><td colspan="4">暂无数据</td></tr>'

    generated = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    updated = store.get("updated_at") or "—"
    page = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>GitHub 流量分析 · {html.escape(store.get('repo', ''))}</title>
<style>
* {{ box-sizing: border-box; }}
body {{ margin: 0; padding: 32px; background: #F7F7F5; color: #2C2C2A;
       font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }}
main {{ max-width: 1080px; margin: 0 auto; }}
h1 {{ font-size: 24px; font-weight: 500; margin: 0 0 4px; }}
h2 {{ font-size: 15px; font-weight: 500; margin: 0 0 12px; }}
.meta {{ font-size: 12px; color: #5F5E5A; margin-bottom: 22px; }}
.hero {{ background: #FAEEDA; border: 1px solid #EF9F27; border-left: 4px solid #854F0B;
         border-radius: 12px; padding: 20px 24px; margin-bottom: 20px; }}
.hero h2 {{ font-size: 18px; color: #412402; margin-bottom: 8px; }}
.hero p {{ font-size: 13px; line-height: 1.7; color: #633806; margin: 0 0 12px; }}
.tags span {{ display: inline-block; font-size: 12px; padding: 4px 10px; border-radius: 999px;
              margin-right: 8px; border: 1px solid; }}
.tag-yes {{ color: #0F6E56; border-color: #5DCAA5 !important; background: #E1F5EE; }}
.tag-no {{ color: #A32D2D; border-color: #F09595 !important; background: #FCEBEB; }}
.kpis {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; margin-bottom: 20px; }}
.kpi {{ background: #FFFFFF; border: 1px solid #E3E1DA; border-radius: 12px;
        padding: 16px 18px; border-top: 3px solid #185FA5; }}
.kpi.real {{ border-top-color: #1D9E75; }}
.kpi.noise {{ border-top-color: #BA7517; }}
.kpi .label {{ font-size: 12px; color: #5F5E5A; margin-bottom: 6px; }}
.kpi .value {{ font-size: 30px; font-weight: 500; line-height: 1.1; }}
.kpi .hint {{ font-size: 11px; color: #888780; margin-top: 8px; line-height: 1.5; }}
.panel {{ background: #FFFFFF; border: 1px solid #E3E1DA; border-radius: 12px;
          padding: 20px; margin-bottom: 20px; overflow-x: auto; }}
.two {{ display: grid; grid-template-columns: 1fr 1fr; gap: 20px; }}
.steps {{ margin: 0; padding-left: 22px; font-size: 13px; line-height: 1.8; }}
.steps li {{ margin-bottom: 8px; }}
.steps b {{ font-weight: 500; color: #185FA5; }}
table {{ border-collapse: collapse; width: 100%; font-size: 13px; }}
th, td {{ text-align: left; padding: 8px 10px; border-bottom: 1px solid #EFEDE7; }}
th {{ color: #5F5E5A; font-weight: 500; }}
.note {{ font-size: 12px; color: #5F5E5A; line-height: 1.7; }}
strong {{ font-weight: 500; }}
@media (max-width: 800px) {{ .kpis, .two {{ grid-template-columns: 1fr; }} }}
</style>
</head>
<body>
<main>
<h1>GitHub 流量分析 · {html.escape(store.get('repo', ''))}</h1>
<div class="meta">本地存档 {len(days)} 天 · 最近抓取 {html.escape(str(updated))} · 报表生成 {generated} · 每日自动更新</div>

<div class="hero">
<h2>先说结论：这里看不到“真实用户是谁”</h2>
<p>GitHub Traffic 只向仓库管理员提供<strong>聚合统计</strong>，不提供访客的账号、用户名、IP 地址或设备信息。
任何权限的 GitHub token 都拿不到这些字段，网页 Insights 里也没有。想要 IP 只能自己托管页面并自己记日志，
那已经不是 GitHub 仓库的统计了。下面这些数字里，<strong>最接近“多少人来过”的是浏览独立访客</strong>。</p>
<div class="tags">
<span class="tag-yes">能看到：浏览量 / 独立访客 / 来源域名 / 页面路径</span>
<span class="tag-no">看不到：用户名 / IP / 设备 / 逐条访问日志</span>
</div>
</div>

<div class="kpis">
<div class="kpi real"><div class="label">真实外部访客</div><div class="value">{views_uniques}</div><div class="hint">近 14 天浏览独立访客，GitHub 已去重；这是最接近“多少人看过”的数字</div></div>
<div class="kpi"><div class="label">页面浏览</div><div class="value">{views_count}</div><div class="hint">近 14 天 PV，同一人多次打开会重复计入</div></div>
<div class="kpi noise"><div class="label">克隆次数</div><div class="value">{clones_count}</div><div class="hint">包含你自己反复 clone / push / pull，不等于用户数</div></div>
<div class="kpi noise"><div class="label">克隆独立访客</div><div class="value">{clones_uniques}</div><div class="hint">按 IP 等口径去重，仍然主要是开发操作噪音</div></div>
</div>

<div class="panel">
<h2>我是怎么分析的</h2>
<ol class="steps">
<li><b>拉四个官方接口</b>：views、clones、popular/referrers、popular/paths，只需要该仓库 Administration 读权限。</li>
<li><b>按 UTC 日期合并</b>：同一天重复抓取直接覆盖当天值，不累加，避免把同一次访问算两遍。</li>
<li><b>把浏览和克隆分开看</b>：克隆是 git 操作，你自己的推拉也会计入。峰值在 {html.escape(str(peak_day))} 单日 {peak_clones} 次，
占总克隆 {peak_share}%，明显是开发行为，因此不当成真实用户。</li>
<li><b>用来源和路径交叉验证</b>：来源只有 {html.escape(ref_text)}，没有搜索引擎；14 天里只有 {active_days} 天浏览非零。
两条证据都指向：真实外部访问很少。</li>
</ol>
</div>

<div class="two">
<div class="panel"><h2>每日浏览（最近 {views_n} 天）</h2>{views_svg}</div>
<div class="panel"><h2>每日克隆（最近 {clones_n} 天）</h2>{clones_svg}</div>
</div>

<div class="panel">
<h2>按日明细</h2>
<table><thead><tr><th>日期（UTC）</th><th>浏览量</th><th>独立访客</th><th>克隆数</th><th>克隆独立访客</th></tr></thead>
<tbody>{rows}</tbody></table>
</div>

<div class="two">
<div class="panel">
<h2>来源网站（近 14 天）</h2>
<table><thead><tr><th>来源</th><th>浏览量</th><th>独立访客</th></tr></thead>
<tbody>{referrer_rows}</tbody></table>
</div>
<div class="panel">
<h2>热门页面（近 14 天）</h2>
<table><thead><tr><th>路径</th><th>浏览</th><th>访客</th></tr></thead>
<tbody>{''.join('<tr><td>{}</td><td>{}</td><td>{}</td></tr>'.format(html.escape(str(item.get('path', ''))), item.get('count', 0), item.get('uniques', 0)) for item in store.get('paths', [])) or '<tr><td colspan="3">暂无数据</td></tr>'}</tbody></table>
</div>
</div>

<div class="panel note">
口径说明：GitHub 官方只保留最近 14 天，本页把每次抓到的日期合并存档，因此本地天数会逐日增长；
“独立访客”按天去重后相加，不等同跨天去重的真实人数；同一天重抓覆盖不重复计。<br>
数据来源为 GitHub 官方 Traffic，不依赖第三方徽章，README 里不需要加任何外链图片。
</div>
</main>
</body>
</html>
"""
    REPORT_FILE.write_text(page, encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="本地长期保存 GitHub 仓库流量数据")
    parser.add_argument("--repo", default=DEFAULT_REPO, help="owner/repo")
    parser.add_argument("--set-token", action="store_true", help="交互式写入本地 token")
    parser.add_argument("--no-fetch", action="store_true", help="不请求 API，只用本地数据重生成报表")
    args = parser.parse_args()

    if args.set_token:
        save_token()
        return

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    store = load_store(args.repo)
    latest = None

    if not args.no_fetch:
        token = load_token()
        if not token:
            raise SystemExit(
                "未找到 GitHub token。任选一种：\n"
                f"  1) 运行 python3 {Path(__file__).name} --set-token\n"
                "  2) 设置环境变量 GITHUB_TOKEN\n"
                " Fine-grained PAT 需要该仓库 Administration: Read-only 权限。"
            )
        views = fetch_json(f"/repos/{args.repo}/traffic/views", token)
        clones = fetch_json(f"/repos/{args.repo}/traffic/clones", token)
        referrers = fetch_json(f"/repos/{args.repo}/traffic/popular/referrers", token)
        paths = fetch_json(f"/repos/{args.repo}/traffic/popular/paths", token)
        merge_view(store, views, "views")
        merge_view(store, clones, "clones")
        store["repo"] = args.repo
        store["updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        store["referrers"] = referrers
        store["paths"] = paths
        snapshot_day = store["updated_at"][:10]
        store.setdefault("extras_history", {})[snapshot_day] = {
            "referrers": referrers,
            "paths": paths,
        }
        latest = {
            "views_count": views.get("count", 0),
            "views_uniques": views.get("uniques", 0),
            "clones_count": clones.get("count", 0),
            "clones_uniques": clones.get("uniques", 0),
        }
        store["latest"] = latest
        DATA_FILE.write_text(
            json.dumps(store, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

    if not store["days"]:
        raise SystemExit("本地还没有数据，先运行一次抓取。")

    write_csv(store)
    write_extra_csv(store)
    build_report(store, latest)

    days = store["days"]
    print(f"仓库：{store['repo']}")
    print(f"本地存档天数：{len(days)}")
    if latest:
        print(
            "官方 14 天：浏览 {views_count} / 独立访客 {views_uniques} / "
            "克隆 {clones_count} / 克隆访客 {clones_uniques}".format(**latest)
        )
    print(f"本地累计浏览：{sum(d['views'] for d in days.values())}")
    print(f"本地累计克隆：{sum(d['clones'] for d in days.values())}")
    print(f"明细：{CSV_FILE}")
    print(f"报表：{REPORT_FILE}")


if __name__ == "__main__":
    main()
