# 王者荣耀拨测执行器

本目录从本机 Game-Agent 的王者项目迁入，接入同仓库采集总控。当前默认尝试路径是已登录游戏 → 大厅 → 单人练习场 → 局内执行 → 结束识别 → 采集后处理。整合版尚未重新执行实机全流程，详细状态见 [实现与验证状态](../../docs/IMPLEMENTATION_STATUS.md)。

## 环境与模型

从仓库根目录执行。王者使用独立 Python 3.10，不与金铲铲/采集环境混装。以下 CUDA 12.1 PyTorch 版本对应本机已有部署；新机器安装及驱动兼容性尚未重新验收：

```powershell
py -3.10 -m venv .\game-agent\wzry\.venv
& .\game-agent\wzry\.venv\Scripts\python.exe -m pip install torch==2.5.1 --index-url https://download.pytorch.org/whl/cu121
& .\game-agent\wzry\.venv\Scripts\python.exe -m pip install -r .\game-agent\wzry\requirements.txt
```

需要另外准备以下文件，相对路径均以本目录为基准：

| 文件 | 用途 |
| --- | --- |
| `models/start.onnx` | 对局开始/局内状态识别 |
| `models/death.onnx` | 死亡状态识别 |
| `src/wzry_ai_bc.pt` | 当前局内策略权重 |

[assets.example.json](assets.example.json) 记录本机快照的文件大小与 SHA-256。**模型未上传，尚无已验证的公共下载入口或自动下载脚本。** 需要由维护者提供获准使用的模型；再分发条件仍待确认。全新 clone 不能直接开始实机拨测。

```powershell
& .\game-agent\wzry\.venv\Scripts\python.exe .\game-agent\wzry\check_runtime.py --verify-hashes
```

此命令只检查模型、依赖导入和 CUDA 可用性，不操作游戏。启动脚本也会执行基础预检，但默认不重复计算约 1 GB 模型的哈希。`-ComputeDevice cpu` 可显式选择 CPU，性能未验证。自定义 `-ModelPath` 仍需与策略网络结构兼容，自定义模型没有快照校验值。

采集环境、配置、ADB、scrcpy、FFmpeg 和设备端 PCAPdroid/tcpdump 按 [根目录部署说明](../../README.md) 准备。当前布局依据雷电 `1920x1080` 校准。生产运行的 ADB 优先级为显式参数、`MINI_PILOT_ADB_BIN`、PATH，最后才尝试旧版随附目录。

## 启动

```powershell
$env:MINI_PILOT_ADB_BIN = 'D:\leidian\LDPlayer14\adb.exe' # 按实际安装修改
.\game-agent\wzry\run_capture_probe.ps1 -DryRun -DeviceId 'emulator-5554' -RunId wzry-preview
.\game-agent\wzry\run_capture_probe.ps1 -DeviceId 'emulator-5554' -RunId wzry-round-01
```

`-DryRun` 只显示命令，不检查依赖，也不执行 ADB。脚本默认使用同仓库采集目录和本目录 `.venv`；可用 `MINI_PILOT_ROOT`、`-GamePython` 覆盖。`-DisablePostprocess` 跳过后处理，报告会注明不完整；`-SkipPcapdroidPreflight` 只供已确认采集方式的环境使用，不能替代抓包验证。

两轮试验入口（代码已实现，本版本实机连续运行未验证）：

```powershell
.\game-agent\wzry\run_capture_probe.ps1 -Rounds 2 -SessionId wzry-two-rounds
```

## 能力边界

- 已登录“开始游戏”页面有识别规则；真实登录、验证码、实名和支付页会请求人工接管。登录到大厅的无人干预闭环尚未验收。
- 活动页仅在标题与角落明确关闭文字同时满足规则时点击；复用本轮 OCR，减少重复识别。纯图标关闭、模板匹配后备策略尚未实现，ROI 关闭速度未做新实测。
- 前置驱动针对训练营/练习场导航；`-PregameGoal` 不是任意自然语言任务引擎，仅影响预设导航模式。单独运行该驱动也不提供“到大厅即停”的专用模式。
- 结束判断可能使用局内信号消失兜底，结果记 `unknown`；不能当作胜负识别成功。
- 画面长时间无法识别时仍有返回键恢复策略，其他分辨率、活动版式和异常断线场景未全面验证。

原研究代码、训练/标注工具和历史文档随源码保留，未全部适配统一部署。例如 `human_data_collector.py` 仍有旧随附 ADB 路径；它不在正式拨测链路中。旧 `README_NEW.md` 和设计笔记描述的是历史实验，不作为本版本验收证据。

接口细节见 [CAPTURE_INTEGRATION.md](CAPTURE_INTEGRATION.md)。迁入前 README 保留在 [README_UPSTREAM.md](README_UPSTREAM.md)，原项目署名与来源见 [来源说明](../../docs/SOURCE_NOTES.md)。
