# 第三方组件许可说明

「轻述 sayo」应用代码以 MIT 许可证发布（见 [`LICENSE`](LICENSE)）。
安装包内随附以下第三方组件，它们各自遵循自己的许可证；本文件仅作汇总说明，
完整许可证文本以各组件随包文件及上游项目为准。

| 组件 | 许可证 | 上游 / 备注 |
| --- | --- | --- |
| sherpa-onnx | Apache-2.0 | k2-fsa/sherpa-onnx |
| SenseVoiceSmall | Apache-2.0 | iic / FunAudioLLM |
| Silero VAD | MIT | snakers4/silero-vad |
| PySide6 / Qt | LGPL-3.0 或 GPL-3.0 | 商业使用请遵循 Qt 条款 |
| PortAudio | MIT | portaudio/portaudio |
| pynput | MIT | mosesbotto/pynput |
| pyperclip | BSD-3-Clause | asweigart/pyperclip |
| pypinyin | MIT | mozman/pypinyin |
| Inno Setup | Inno Setup License | 仅用于生成 Windows 安装器（编译产物，不修改其源码） |

模型权重与第三方库**不随源码仓库分发**；安装包构建时会把运行时依赖打入包内，语音模型由用户首次获取：
macOS 运行 `scripts/install_models.command`，Windows 由应用首启时在后台下载（逐文件校验 SHA-256）。

### 关于 Qt / PySide6 的 LGPL 合规

本项目的 Windows / macOS 安装包以 **LGPL-3.0** 方式使用 Qt（经 PySide6）：

- Qt 库以**动态链接**形式随包（安装目录 `_internal` 下的 `Qt6*.dll` / `.dylib`，macOS 为 `.app` 内的 framework），
  未与应用程序静态合并，用户可以用自己的 Qt 版本替换这些文件。
- 需要重新链接/替换时：删除或改名上述 Qt 库文件，再放入自行编译的同版本库即可，应用无需重新签名以外的改动。
- Qt 源码可从 <https://download.qt.io/> 获取；LGPL-3.0 全文见 <https://www.gnu.org/licenses/lgpl-3.0.html>。

若你分发修改后的安装包，请同时保留本文件与各组件原有的许可证/版权声明。
