## 改动内容

一句话说明改了什么，以及为什么改。

## 关联 Issue

Closes #

## 验证

- [ ] `SAYO_DATA=/tmp/sayo-test python -m pytest -q tests`
- [ ] `python -m app.main --self-check`（本机有模型时）
- [ ] macOS 真机听写冒烟（如涉及热键 / 音频 / 浮层）

## 注意事项

- 运行时代码不得新增任何网络请求；模型下载只能在独立脚本中完成。
- 只改与本问题相关的代码，不顺手重构无关模块。
- 提交标题简短，根因 / 修法 / 验证写在正文或关联 Issue 中。
