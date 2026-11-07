#!/usr/bin/env python3
"""离线验收工具：模型完整性 + 源码联网审计 + 运行时断网实测 + 中英文识别冒烟测试。

用法：
    python3 scripts/verify_offline.py                 # 全量验收
    python3 scripts/verify_offline.py --check-models # 只校验模型校验和
    python3 scripts/verify_offline.py --json         # 输出机器可读结果
"""
from __future__ import annotations

import argparse
import hashlib
import json
import socket
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def check_models() -> dict:
    manifest = json.loads((ROOT / "models" / "manifest.json").read_text(encoding="utf-8"))
    results = []
    for rel, meta in manifest["files"].items():
        p = ROOT / "models" / rel
        if not p.is_file():
            if meta.get("optional"):
                results.append({"file": rel, "ok": True, "skipped": True,
                                "reason": "可选文件未随 --lite 构建分发"})
            else:
                results.append({"file": rel, "ok": False, "reason": "文件缺失"})
            continue
        size = p.stat().st_size
        digest = sha256(p)
        ok = digest == meta["sha256"] and size == meta["bytes"]
        results.append({"file": rel, "ok": ok, "bytes": size,
                        "sha256": digest, "reason": "" if ok else "校验和/大小不匹配"})
    return {"ok": all(r["ok"] for r in results), "files": results}


def audit() -> dict:
    from app.core.offline import audit_source
    hits = audit_source(str(ROOT / "app"))
    return {"ok": not hits, "hits": hits}


def runtime_test() -> dict:
    """进程级封锁所有 IP 连接后，跑一遍真实 ASR，证明识别不需要网络。"""
    from app.core.offline import NetworkBlocked, enforce_offline_lock
    enforce_offline_lock()
    blocked = False
    try:
        socket.create_connection(("1.1.1.1", 443), timeout=0.2)
    except (NetworkBlocked, OSError):
        blocked = True

    fixture = ROOT / "tests" / "fixtures" / "zh_sample.wav"
    if not fixture.is_file():
        return {"ok": False, "network_blocked": blocked, "reason": "缺少语音样本"}
    try:
        from app.config import models_dir
        from app.core.asr import ASREngine
        from app.core.recorder import read_wav
        engine = ASREngine(models_dir(), {
            "model": "sensevoice", "language": "auto", "num_threads": 2,
            "vad_enabled": True, "vad_threshold": 0.42,
            "vad_min_silence": 0.55, "vad_min_speech": 0.18,
        })
        ok, msg = engine.check_models()
        if not ok:
            return {"ok": False, "network_blocked": blocked, "reason": msg}
        r = engine.transcribe(read_wav(fixture))
        return {"ok": blocked and bool(r.text), "network_blocked": blocked,
                "text": r.text, "language": r.language, "elapsed_ms": r.elapsed_ms,
                "backend": r.backend, "precision": r.precision}
    except Exception as exc:
        return {"ok": False, "network_blocked": blocked, "reason": str(exc)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check-models", action="store_true")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    models = check_models()
    report: dict = {"models": models}
    if not args.check_models:
        report["source_audit"] = audit()
        report["runtime"] = runtime_test()
    passed = models["ok"] and all(
        v.get("ok", False) for k, v in report.items() if k != "models"
    ) if not args.check_models else models["ok"]

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print("=== 离线验收 ===")
        for r in models["files"]:
            suffix = "（可选，未打包）" if r.get("skipped") else f"({r.get('bytes','-')} bytes)"
            print(f"[{'OK' if r['ok'] else 'FAIL'}] {r['file']} {suffix} {r['reason']}")
        if not args.check_models:
            a = report["source_audit"]
            print(f"[{'OK' if a['ok'] else 'FAIL'}] 源码联网调用扫描：{len(a['hits'])} 处")
            rt = report["runtime"]
            print(f"[{'OK' if rt['ok'] else 'FAIL'}] 断网实测：网络封锁={rt['network_blocked']}，"
                  f"识别结果={rt.get('text') or rt.get('reason')}，耗时={rt.get('elapsed_ms','-')} ms，"
                  f"后端={rt.get('backend','-')}/{rt.get('precision','-')}")
        print("结论：", "通过 · 可在完全断网环境运行" if passed else "未通过 · 请勿发布该安装包")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
