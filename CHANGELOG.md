# 更新日志

## [Unreleased]

- 新增 GitHub Traffic 本地归档脚本（`scripts/github_traffic_local.py`）。
- 补齐 Issue / PR 模板、贡献指南、安全策略与 CI。
- 为 Release 安装包补充 `.sha256` 校验文件，统一 README 与 Release 文案。
- 将第三方组件许可从 `LICENSE` 拆分到 `THIRD_PARTY_NOTICES.md`。

## [v1.0.0] - 2026-11-24

首个正式版。已归档并关闭的历史问题：

- [#1](https://github.com/hlc-369123/sayo/issues/1) 音频流停止死锁导致界面卡死、系统静音无法恢复
- [#2](https://github.com/hlc-369123/sayo/issues/2) 双实例互抢麦克风导致完全识别不出字
- [#3](https://github.com/hlc-369123/sayo/issues/3) 默认输入设备断流导致完全识别不出字
- [#4](https://github.com/hlc-369123/sayo/issues/4) 输入监控授权误报（IOHIDCheckAccess 常量错误）
- [#5](https://github.com/hlc-369123/sayo/issues/5) 单键 Option 热键无法录入或触发
- [#6](https://github.com/hlc-369123/sayo/issues/6) 快捷键录入框录不进系统级组合键
- [#7](https://github.com/hlc-369123/sayo/issues/7) 快捷键录入失效与按键后迟迟不开始录音
- [#8](https://github.com/hlc-369123/sayo/issues/8) 改坏一个快捷键导致全部热键失效
- [#9](https://github.com/hlc-369123/sayo/issues/9) 开始提示音被静音吞掉 + 长句整段无标点
- [#10](https://github.com/hlc-369123/sayo/issues/10) 句首「这个/那个」被当填充词误删导致吞字
- [#11](https://github.com/hlc-369123/sayo/issues/11) 通篇句号 + Spotlight 搜索出现两个应用
- [#12](https://github.com/hlc-369123/sayo/issues/12) SenseVoice 散点句号与标点规则兜底增强
- [#13](https://github.com/hlc-369123/sayo/issues/13) 胶囊悬停提示不弹出 / 不灵敏
- [#14](https://github.com/hlc-369123/sayo/issues/14) 胶囊右缘拉伸时切回桌面
- [#15](https://github.com/hlc-369123/sayo/issues/15) 切换浮层样式误弹窗、经典模式不显示文字
- [#16](https://github.com/hlc-369123/sayo/issues/16) 交互提示音不响（资源路径计算错误）
- [#17](https://github.com/hlc-369123/sayo/issues/17) 推送脚本误判 GitHub 认证失败与断点续推

### 源码与安装包对应关系

- 标签 `v1.0.0` 指向提交 `0756f47`（音频流停止死锁根治）。
- 2026-11-24 构建的安装包还包含随后提交的 `0f1b0a8`（单键 Option 热键修复）。
- 该提交及其后的源码已推送到 `main`，可从源码完整复现安装包行为。
