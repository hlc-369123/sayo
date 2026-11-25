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

模型权重与第三方库**不随源码仓库分发**；安装包构建时会打入运行时依赖，
模型由用户首次运行时通过 `scripts/install_models.command` 单独下载到用户数据目录。

若你分发修改后的安装包，请同时保留本文件与各组件原有的许可证/版权声明。
