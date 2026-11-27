# 更新日志

## [v1.0.1] - 2026-11-27

macOS 正式版。合入 Mac 侧代码评审修复的 13 项问题，CI 全绿、169 项测试通过（安装后自检与 `--file` 识别均验证通过）。

- 修复音频流停止死锁导致的界面卡死、系统静音无法恢复（CallbackAbort + finished_callback 根治）。
- 修复双实例互抢麦克风、默认输入设备断流导致的识别不出字；增加 GUI 单实例锁。
- 修复快捷键录入失效、单键 Option 触发、改坏一个热键导致全部失效等问题。
- 修复静音会话时序（generation token 防迟到定时器）、开始提示音被吞、utterance 标点。
- 设置窗口大模型状态检查改为文件级判断，不再每次加载 1.9 GB GGUF（内存占用显著降低）。
- ASR 耗时独立统计；设置保存后实时引擎配置同步；粘贴流程移出 GUI 线程。
- 版本号改为单一来源（`app/config.py` 的 `APP_VERSION`），spec 与构建脚本统一读取。

本次同时发布 macOS 与 Windows 安装包，两个平台的版本号、校验文件与安装方式各自独立。

### Windows 平台支持（新增）

- 同一份源码支持 Windows 10 1809+ / 11 x64：Inno Setup 安装包（`sayo-*-windows-setup.exe`）与免安装 ZIP 两种形态，安装/卸载都不需要管理员权限。
- **默认听写键改为单按 `Alt`**；快捷键录入改用低层键盘钩子，支持单独录 `Alt` / `Ctrl` / `Shift` / `Win` 或任意组合键（旧配置自动迁移一次）。
- 安装包**不捆语音模型**：首次启动由应用在后台下载并逐个校验 SHA-256，失败重试 2 次、下次启动再试，成功后不再请求；设置 → 识别 里可手动下载并查看进度，下载完成后热切换引擎、无需重启。
- 提示音改为单线程队列 + 阻塞播放：开始/结束音各自完整播完，不再被下一段打断，也不再和录音输入流抢音频设备。
- 平台文案与图标彻底分开：新增 `app/platform/platform_text.py` 作为唯一文案出口，Windows 界面不再出现 `⌘`、`/Applications`、`辅助功能` 等 macOS 字样；Windows 使用独立图标 `assets/icon-windows.ico`，不覆盖 macOS 的 `icon.png` / `icon.icns`。
- 卸载程序安装后改名为 `卸载sayo.exe`（含同名 `.dat`），注册表卸载入口同步改写；安装目录不再出现 `unins000.*`。
- 覆盖安装会先静默结束正在运行的实例，装完自动刷新图标缓存，桌面只留 1 个启动图标。
- 新增 `.github/workflows/release.yml`：推 `v*` 标签时 macOS 与 Windows 各构建各的，产物合并挂到同一个 Release；`ci.yml` 增加 Windows 测试作业。
- 新增 `scripts/install_sayo.ps1` / `scripts/install_sayo.sh`：按平台只下载自己那一个资产，并校验同名 `.sha256`。
- 版本号统一取 `app/config.py` 的 `APP_VERSION`，构建脚本与安装器不再各自写死。
- `build_windows.ps1` 对 PyInstaller / Inno Setup 的退出码做 fail-fast 检查：打包失败立即中止，不再静默沿用上一次的旧产物（表现为安装包哈希不变却看不出报错）；受限环境里改为先把旧 `dist\sayo` 重命名挪走，避开「批量删除被拦」。

## [Unreleased]

### 其他

- 历史问题 Issue 已按「问题发生时间从早到晚」重建编号（#18–#34）：最新的死锁问题现在是 #34，默认排序下排在最上方；原 #1–#17 保留为归档记录。
- 新增 GitHub Traffic 本地归档脚本（`scripts/github_traffic_local.py`）。
- 补齐 Issue / PR 模板、贡献指南、安全策略与 CI；CI 使用 `macos-15-intel`，跳过需本地模型的真实 ASR 用例与对时序敏感的实时窗口用例（这两条仍在本地全量测试中覆盖）。
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

- 标签 `v1.0.0` 指向提交 `0756f47`（音频流停止死锁根治）；2026-11-24 构建的安装包还包含随后提交的 `0f1b0a8`（单键 Option 热键修复）。
- 标签 `v1.0.1` 指向提交 `eba68f3`，包含 Mac 侧评审修复的 13 项问题与版本号单一来源改动，是当前最新的 macOS 正式版。
- Windows 安装包仍为 v1.0.0（Windows 适配分支验收中）；macOS 用户请下载 v1.0.1。
