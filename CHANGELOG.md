# 更新日志

## [Unreleased]

- 历史问题 Issue 已按「问题发生时间从早到晚」重建编号（#18–#34）：最新的死锁问题现在是 #34，默认排序下排在最上方；原 #1–#17 保留为归档记录。
- 新增 GitHub Traffic 本地归档脚本（`scripts/github_traffic_local.py`）。
- 补齐 Issue / PR 模板、贡献指南、安全策略与 CI；CI 使用 `macos-15-intel`，跳过需本地模型的真实 ASR 用例（本地仍可跑 `scripts/verify_offline.py`）。
- 为 Release 安装包补充 `.sha256` 校验文件，统一 README 与 Release 文案。
- 将第三方组件许可从 `LICENSE` 拆分到 `THIRD_PARTY_NOTICES.md`。

## [v1.0.0] - 2026-11-24

首个正式版。历史问题按发生时间从早到晚编号：

- [#18](https://github.com/hlc-369123/sayo/issues/18) 句首「这个/那个」被当填充词误删导致吞字
- [#19](https://github.com/hlc-369123/sayo/issues/19) 胶囊悬停提示不弹出 / 不灵敏
- [#20](https://github.com/hlc-369123/sayo/issues/20) 胶囊右缘拉伸时切回桌面
- [#21](https://github.com/hlc-369123/sayo/issues/21) 切换浮层样式误弹窗、经典模式不显示文字
- [#22](https://github.com/hlc-369123/sayo/issues/22) 交互提示音不响（资源路径计算错误）
- [#23](https://github.com/hlc-369123/sayo/issues/23) 推送脚本误判 GitHub 认证失败与断点续推
- [#24](https://github.com/hlc-369123/sayo/issues/24) 双实例互抢麦克风导致完全识别不出字
- [#25](https://github.com/hlc-369123/sayo/issues/25) 默认输入设备断流导致完全识别不出字
- [#26](https://github.com/hlc-369123/sayo/issues/26) 开始提示音被静音吞掉 + 长句整段无标点
- [#27](https://github.com/hlc-369123/sayo/issues/27) 通篇句号 + Spotlight 搜索出现两个应用
- [#28](https://github.com/hlc-369123/sayo/issues/28) SenseVoice 散点句号与标点规则兜底增强
- [#29](https://github.com/hlc-369123/sayo/issues/29) 快捷键录入框录不进系统级组合键
- [#30](https://github.com/hlc-369123/sayo/issues/30) 快捷键录入失效与按键后迟迟不开始录音
- [#31](https://github.com/hlc-369123/sayo/issues/31) 改坏一个快捷键导致全部热键失效
- [#32](https://github.com/hlc-369123/sayo/issues/32) 单键 Option 热键无法录入或触发（macOS flagsChanged 限制）
- [#33](https://github.com/hlc-369123/sayo/issues/33) 修复输入监控授权误报（IOHIDCheckAccess 常量错误）
- [#34](https://github.com/hlc-369123/sayo/issues/34) 音频流停止死锁导致界面卡死、系统静音无法恢复（最新）

### 源码与安装包对应关系

- 标签 `v1.0.0` 指向提交 `0756f47`（音频流停止死锁根治）。
- 2026-11-24 构建的安装包还包含随后提交的 `0f1b0a8`（单键 Option 热键修复）。
- 该提交及其后的源码已推送到 `main`，可从源码完整复现安装包行为。
- 因此**线上安装包就是最新应用代码**；没有新的代码修复，故不另发 `v1.0.1`、不重新打包。
