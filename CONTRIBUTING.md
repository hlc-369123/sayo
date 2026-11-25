# 参与贡献

欢迎提 Issue、修 bug、补文档。本项目坚持**运行时完全离线**，任何需要联网、登录、遥测或自动更新的功能都不会被接受。

## 开发环境

- macOS（热键 / 音频 / 权限相关代码只能在 macOS 上真机验证）
- Python 3.11+
- 首次准备：

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements-macos.txt -r requirements-build.txt
bash scripts/fetch_models.sh     # 仅开发机需要，约 240 MB
```

## 分支与提交

- 从 `main` 切分支：`git checkout -b fix/简短描述`
- **提交标题一行写清楚**，根因 / 修法 / 验证写在提交正文或关联 Issue 中。
- 一个问题一个提交/PR，不要夹带无关改动。
- 用 `Closes #编号` 关联 Issue。

## 本地验证

```bash
SAYO_DATA=/tmp/sayo-test python -m pytest -q tests
python scripts/verify_offline.py
python -m app.main --self-check
```

涉及热键、录音、浮层、权限的改动，必须在真机上做一次完整听写冒烟测试。

## 文档

- 用户向：`docs/使用手册.md`
- 开发向：`docs/开发指南.md`
- 行为边界：`README.md` 的「离线承诺」

## 报告问题

用 Issue 模板反馈，尽量附复现步骤与 `~/Library/Application Support/sayo/logs/` 中的相关日志（隐去隐私）。
安全问题不要公开 Issue，按 [`SECURITY.md`](SECURITY.md) 的方式私下反馈。
