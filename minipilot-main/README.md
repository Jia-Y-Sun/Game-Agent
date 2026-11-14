# MiniPilot

MiniPilot 是一个由视觉语言模型驱动的 Android 自动化与业务样本采集工具。它根据用户目标和实时手机截图生成 ADB 操作，并可在同一次任务中同步采集网络流量、录屏、系统日志和 QoE 数据。

## 主要功能

- 使用兼容 OpenAI Chat Completions 接口的视觉语言模型操作 Android 设备。
- 支持命令行和桌面 GUI。
- 支持运行中发送补充指令、一次性指令或停止指令。
- 支持 Agent 自动操作和人工操作两种模式。
- 集成 Unicapture，同步生成 PCAP、MP4、LOG、YAML、SA CSV 和 QoE CSV。
- 支持无 Root 的 PCAPdroid 抓包和 Root 设备的 tcpdump 抓包。
- 采集结束后自动执行业务时间识别、QoE 后处理和流级标注。

## 系统要求

- Python 3.9 或更高版本。
- Android Platform Tools，且 `adb` 已加入 PATH。
- 已开启 USB 调试的 Android 设备。
- 一个支持图像输入的 OpenAI 兼容模型服务。
- AutoGLM、Seed LLM Agent 等 Executor 执行文本输入时，手机端必须安装并启用 ADBKeyboard。
- 无 Root 采集网络流量时，手机端必须安装项目配套的定制 PCAPdroid；也可使用具备 Root 权限和 tcpdump 的设备。
- 长时间 Agent 任务需要开启 USB 调试安全设置、保持唤醒，并延长自动锁屏和熄屏时间。

完整环境、手机端和抓包配置见[安卓端侧 APP 样本采集方案](安卓端侧APP样本采集方案.md)。

## 快速开始

创建虚拟环境并安装依赖：

```bash
python -m venv .venv
```

Windows PowerShell：

```powershell
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item mini_pilot.config.example.json mini_pilot.config.json
```

macOS/Linux：

```bash
source .venv/bin/activate
python -m pip install -r requirements.txt
cp mini_pilot.config.example.json mini_pilot.config.json
```

编辑 `mini_pilot.config.json`，选择 `autoglm` 或 `seed2pro` Executor 档案并填写对应平台的 Key；另外必须填写阿里云百炼 Qwen-VL 的 `postprocess_vlm_base_url`、`postprocess_vlm_model` 和 `postprocess_vlm_api_key`，以及手机端 `capture.pcapdroid_api_key`。具体申请入口和字段值见[采集方案的模型配置章节](安卓端侧APP样本采集方案.md#54-autoglm-phone智谱开放平台)和[Qwen-VL 配置章节](安卓端侧APP样本采集方案.md#57-后处理-vlmqwen-vl阿里云百炼)。然后执行检查（将 Executor 档案名替换为实际选择）：

```bash
python model_ping.py --target autoglm
python model_ping.py --target postprocess_vlm
python -m mini_pilot.main --list-devices
python -m mini_pilot.main --check-device
```

查询内置采集 APP 及其场景名，或查看某个 APP 的场景说明和默认时长：

```bash
python -m mini_pilot.main --list-apps
python -m mini_pilot.main --list-scenes douyin
```

启动 GUI：

```bash
python -m mini_pilot.gui
```

或者直接运行命令行任务：

```text
python -m mini_pilot.main --goal "打开抖音浏览 5 分钟" --run-duration 300 --capture unicapture --capture-app douyin --capture-scene feed
```

## 项目结构

```text
mini_pilot/                 Android Agent、GUI、模型和采集编排
unicapture/                 录屏、抓包、日志、QoE 和后处理
tests/                      自动化测试
runs/                       每次任务的输出目录，不纳入发布包
mini_pilot.config.example.json
                            用户配置模板
requirements.txt            统一 Python 依赖
package_project.py           发布包生成脚本
安卓端侧APP样本采集方案.md    完整配置与采集方案
极简操作手册.md               日常操作步骤
```

## 输出

每次运行都会在 `runs/<时间戳>-<任务标识>/` 下创建独立目录：

```text
input/                      原始目标和实时指令
execution/                  执行摘要与 Agent 截图
capture/                    Unicapture 日志、采集样本和后处理结果
```

采集可能生成 PCAP、MP4、LOG、YAML、SA CSV 和 QoE CSV。流级标注开启时，会在原始 `*_sa.csv` 旁新建 `*_flow_labeled.csv`；原始 SA 文件和已有标注文件均不会被覆盖。某个后处理组件不可用时，原始采集结果仍会保留，并在 `postprocess/summary.json` 中记录失败原因。

流级标注默认开启，可在配置中关闭：

```json
{
  "capture": {
    "enable_flow_labeling": false
  }
}
```

也可以对单次命令使用 `--capture-disable-flow-labeling`。规则、标签字段和判定顺序见 [流级标注说明](unicapture/postprocess/README_FLOW_LABELING.md)。

## 文档

- [安卓端侧 APP 样本采集方案](安卓端侧APP样本采集方案.md)：系统组成、环境配置、参数解释、完整流程和故障处理。
- [极简操作手册](极简操作手册.md)：完成日常采集所需的连续操作步骤。
- [流级标注说明](unicapture/postprocess/README_FLOW_LABELING.md)：采集后的逐流标签、输出文件和规则配置。

## 打包

```bash
python package_project.py
```

脚本在 `dist/` 中生成 ZIP，并自动排除 Git、虚拟环境、运行数据、缓存、本地配置和密钥文件。
