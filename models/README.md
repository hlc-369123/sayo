# 离线语音模型（不随安装包分发）

**安装包不再包含本目录**：`packaging/sayo.spec` 只打包 `assets/`。
最终用户首次使用前运行 `scripts/install_models.command`，把模型下载到
`~/Library/Application Support/sayo/models/`（应用会优先读取该目录）。
本目录只用于**本地开发 / 从源码运行**，由 `scripts/fetch_models.sh` 获取。
下载完成后应用运行时完全离线、不需要账号、不再联网。

| 文件 | 体积 | 用途 |
| --- | --- | --- |
| `sensevoice/model.onnx` | 894 MiB | **默认档**：fp32 全精度主识别模型（中/英/粤/日/韩，自动语种、标点、ITN）。轻声、长句、噪声环境更稳 |
| `sensevoice/model.int8.onnx` | 228 MiB | **极速档**：int8 量化模型，冷启动更快、内存更省，可在设置中随时切换 |
| `sensevoice/tokens.txt` | 309 KiB | 词表（两档共用） |
| `silero_vad.onnx` | 629 KiB | 语音端点检测，剔除静音、切分长语音；阈值随灵敏度档调整 |
| `whisper/`（可选） | 约 150 MB 起 | 构建时加 `--with-whisper` 才打包，用于 100+ 语种 |

## 精度与 GPU 的关系（重要）

GPU / Apple 神经引擎只提供**算力**，不会改变同一份权重的识别结果。提供两档权重，
是为了让「准确率」来自可验证的模型精度，而不是营销话术：

- **fp32 全精度**（默认）：保留完整浮点权重，量化误差为零；本机实测在 CPU 上 4.5 秒音频
  推理约 56–70 ms，与 Core ML 的 53–67 ms 基本持平，因此无需 GPU 也能实时。
- **int8 量化**：体积约 1/4，适合内存/磁盘受限的机器，识别质量接近 fp32。
- **GPU 后端**：sherpa-onnx 支持 `coreml`（macOS）与 `cuda`（NVIDIA）。设置里可强制启用，
  加载失败自动回退 CPU；`--benchmark` 可在任意机器上实测四种组合。
- **灵敏度**：由 VAD 阈值 / 分段档位 / AGC / 低电平片段二次复核决定，与 GPU 无直接关系；
  额外算力让高灵敏档的复核解码保持实时。

默认只下载 int8（省约 894 MiB），应用会自动回退并正常工作；
`install_models.command --full` 才额外下载 fp32。`manifest.json` 将 fp32 标记为可选，校验脚本不会误报。

## 许可与来源

- `sherpa-onnx`：Apache-2.0（k2-fsa）
- `SenseVoiceSmall` / ONNX fp32、int8 导出：Apache-2.0（iic / FunAudioLLM）
- `Silero VAD`：MIT

上游地址仅用于**下载脚本**获取模型，运行时代码不引用这些地址。

## 完整性校验

```bash
python3 scripts/verify_offline.py --check-models
```

会按 `manifest.json` 逐个比对 SHA-256 与文件大小，用于下载后验收 / 离线介质（U 盘、内网镜像）验收。
`install_models.command` 对用户目录里的模型做同样的校验。
