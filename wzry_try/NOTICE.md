# 来源与第三方声明

## 1. 上游项目

本项目的雏形、部分模块设计与启发式实现来自开源项目：

- **myBoris/wzry_ai** — <https://github.com/myBoris/wzry_ai>
  提供了 DQN 多分支动作空间、开局走线、小地图/目标启发式、行为克隆、设备控制等基础实现。

本项目在其基础上做了大量改写（见 [CHANGELOG.md](CHANGELOG.md) 与 [docs/architecture.md](docs/architecture.md)），
上游代码的版权与权利仍归原作者所有；本仓库的 [LICENSE](LICENSE) 仅覆盖新增与改写的部分。

## 2. 参考思路

- `autowzry` 的对局内兜底交互思路（摇杆长按 + 周期补普攻/技能），仅借鉴工程结构，未复制其策略。

## 3. 第三方依赖与工具

| 组件 | 用途 | 许可 |
| --- | --- | --- |
| [PyTorch](https://pytorch.org/) | 策略网络训练/推理 | BSD-3-Clause |
| [ONNX Runtime](https://onnxruntime.ai/) | 检测模型推理 | MIT |
| [OpenCV](https://opencv.org/) (`opencv-contrib-python`) | 图像处理 | Apache-2.0 |
| [PyQt5](https://riverbankcomputing.com/software/pyqt/) | 截图（窗口抓取）与坐标查看器 | **GPLv3 / 商业授权** |
| [pywin32](https://github.com/mhammond/pywin32) | Windows 窗口句柄 | PSF-2.0 |
| [ppocr-onnx](https://pypi.org/project/ppocr-onnx/) | 胜负 OCR | Apache-2.0（基于 PaddleOCR） |
| [filelock](https://github.com/tox-dev/py-filelock) | 训练数据文件锁 | Unlicense |
| [pyminitouch](https://github.com/williamfzc/pyminitouch)（可选） | `persistent_minitouch` 摇杆后端 | MIT |
| [scrcpy](https://github.com/Genymobile/scrcpy) | 投屏 + adb 输入 | Apache-2.0 |

> **PyQt5 是 GPLv3**：只在本地跑代码不受影响；一旦你分发本项目的二进制或打包产物，
> 需要同时满足 GPLv3（开源你的整体作品）或购买 Riverbank 商业授权。若不想受此约束，
> 可以替换 `android_tool.screenshot_window()` 的实现（例如改用 `adb exec-out screencap`）。

## 4. 资源文件

仓库**不包含**以下文件（体积大或第三方分发受限），需使用者自行准备：

| 文件 | 来源 |
| --- | --- |
| `models/start.onnx`、`models/death.onnx` | 上游项目的模型发布页 / 社区分享 |
| `scrcpy-win64-v2.0/` | scrcpy 官方 release 解压 |
| `src/wzry_ai.pt` | 本项目训练产出（可从头训练，无需预置） |
