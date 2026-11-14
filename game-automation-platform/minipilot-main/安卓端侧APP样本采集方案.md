# 安卓端侧 APP 样本采集方案

本文是 MiniPilot 集成采集的完整配置和实施说明。所有参数、命令和输出均以当前代码实现为准。日常使用若不需要理解系统组成，可直接阅读[极简操作手册](极简操作手册.md)。

## 1. 目标与范围

MiniPilot 在 Android 真机上执行用户指定的业务操作，同时由 Unicapture 采集以下证据：

- 网络流量：PCAP 或 PCAPNG。
- 屏幕录制：优先使用 scrcpy，找不到时回退到 Android `screenrecord`。
- 系统日志：持续采集 `adb logcat`。
- 实时 QoE：按秒采集画面，识别卡顿、分辨率或游戏时延。
- 样本元数据：设备、应用、场景、时间和文件关联信息。
- 采集后标注：识别应用激活时间、实际业务开始时间，并生成最终 QoE CSV。

系统支持两种操作方式：

- `agent`：视觉语言模型读取截图并通过 ADB 操作手机。
- `manual`：MiniPilot 只负责采集和计时，用户直接操作手机，不调用 Agent。

## 2. 系统组成

```text
用户目标 / GUI / CLI
        |
        v
MiniPilot 主进程
  |- 模型客户端：向视觉语言模型发送目标和截图
  |- Agent：解析模型动作并调用 ADB
  |- 运行控制：设备选择、时长、重复执行、实时指令
  `- Unicapture 编排：启动和停止采集子进程
        |
        |- PCAPdroid 或 tcpdump：网络流量
        |- scrcpy 或 screenrecord：屏幕录制
        |- adb logcat：系统日志
        |- QoE Monitor：实时秒级指标
        `- Postprocess：业务时间与最终 QoE 标注
                |
                v
        runs/<本次运行>/
```

关键代码位置：

| 组成 | 位置 | 作用 |
|---|---|---|
| 主入口 | `mini_pilot/main.py` | 解析参数、创建运行目录并协调 Agent 与采集。 |
| GUI | `mini_pilot/gui.py` | 选择设备、应用和场景，启动任务并发送实时指令。 |
| Agent | `mini_pilot/agent.py` | 截图、模型推理、动作执行和任务状态判断。 |
| 设备控制 | `mini_pilot/device.py` | ADB 设备检查、截图、启动和关闭应用。 |
| 采集适配 | `mini_pilot/capture.py` | 启动 Unicapture、验证 PCAPdroid Key、传递参数。 |
| 采集器 | `unicapture/app_collector.py` | 录屏、抓包、logcat、QoE、文件收集和元数据生成。 |
| 应用目录 | `unicapture/app_configs.py` | 支持的 APP、包名、场景、默认时长和 QoE 类型。 |
| 后处理 | `unicapture/postprocess_runner.py` | 业务时间标注、最终 QoE 生成和流级标注。 |

## 3. 环境要求

### 3.1 电脑端

必须安装：

1. Python 3.9 或更高版本。
2. Android Platform Tools；在终端执行 `adb version` 必须成功。
3. 根目录 `requirements.txt` 中的 Python 包。

强烈建议安装：

- `ffmpeg` 和 `ffprobe`：校验录屏、解码视频并为离线 QoE 提取画面。缺失时会回退到 OpenCV，但视频完整性检查能力会下降。
- `scrcpy`：用于超过 180 秒的可靠长录屏。程序依次查找当前目录、PATH、`unicapture/scrcpy/` 等位置；找不到时回退到 Android `screenrecord`，后者通常最多录制 180 秒。

可选安装：

- Tesseract OCR 和 Python 包 `pytesseract`：增强实时 QoE 的文字识别。它不是强制依赖；离线后处理使用 `rapidocr-onnxruntime`。
- Tk：仅 GUI 需要。Windows 和 macOS 官方 Python 通常自带；部分 Linux 发行版需单独安装 `python3-tk`。

### 3.2 Android 设备

#### 开启开发者选项

1. 打开手机“设置 > 关于手机”，连续点击“版本号”或“系统版本”约 7 次，直到提示已进入开发者模式。
2. 进入“设置 > 系统/更多设置 > 开发者选项”。不同厂商的菜单名称可能不同。
3. 打开“USB 调试”。
4. 如果系统提供以下选项，也需要打开：
   - “USB 调试（安全设置）”：小米 MIUI/HyperOS 等系统常见，未开启时 ADB 点击、输入或权限操作可能被拦截。
   - “通过 USB 安装”或“USB 安装”：用于通过 `adb install` 安装 ADBKeyboard 和定制 PCAPdroid。
   - “保持唤醒/不锁定屏幕”：充电时保持屏幕开启，适合长时间 Agent。
5. 将目标 APP 和 PCAPdroid 的电池策略设为“不限制”，允许后台运行；关闭省电模式、超级省电和可能终止 VPN/后台进程的系统优化。
6. 安装并登录目标 APP；在受控测试设备上把自动锁屏和锁屏后自动上锁时间调到最长。不要在不符合实验室安全策略的设备上移除锁屏密码。

#### USB 首次连接

1. 使用支持数据传输的 USB 线直接连接电脑，避免不稳定的扩展坞或仅充电线。
2. 手机 USB 用途选择“文件传输/Android Auto”或其他允许数据连接的模式。
3. 保持手机解锁，看到 RSA 调试授权弹窗时勾选“始终允许使用这台计算机进行调试”，然后允许。
4. 在电脑执行：

   ```text
   adb kill-server
   adb start-server
   adb devices -l
   ```

5. 目标设备必须显示为 `device`：
   - `unauthorized`：解锁手机并接受 RSA 授权；没有弹窗时，在开发者选项中撤销 USB 调试授权后重新连接。
   - `offline`：重新插拔数据线，再执行 `adb kill-server` 和 `adb start-server`。
   - 没有设备：更换数据线/USB 口，确认驱动、USB 模式和 USB 调试设置。
6. 最后执行 MiniPilot 检查：

   ```text
   python -m mini_pilot.main --list-devices
   python -m mini_pilot.main --check-device
   ```

当只连接一台状态为 `device` 的设备时，MiniPilot 会自动选择它。连接多台设备时，必须通过 GUI 选择设备，或在命令中传入 `--device-id <序列号>`；手工 ADB 命令使用 `adb -s <序列号> ...`。

#### 无线 ADB（可选）

长时间采集优先使用稳定的 USB 连接并保持充电。确需无线调试时，电脑和手机必须在可信的同一局域网。

Android 11 及以上可在“开发者选项 > 无线调试”中选择“使用配对码配对设备”，然后执行：

```text
adb pair <手机IP>:<配对端口>
adb connect <手机IP>:<连接端口>
adb devices -l
```

支持旧式 TCP/IP 调试的设备可以先通过 USB 执行：

```text
adb tcpip 5555
adb connect <手机IP>:5555
adb devices -l
```

无线网络切换、休眠或 IP 变化都会中断连接，不建议用于无人值守的关键长任务。

#### 长时间 Agent 的防锁屏设置

开始长任务前，先在手机界面中记录“自动熄屏”“锁屏后自动上锁”“保持唤醒”和电池策略的原值，便于任务结束后恢复。然后按界面完成以下设置：

1. 进入“设置 > 显示与亮度 > 自动锁屏/休眠”，选择“永不”；没有“永不”时选择系统允许的最长时间。
2. 进入“设置 > 密码与安全/锁屏 > 锁屏后自动上锁”，选择最长时间。
3. 进入“开发者选项”，打开“保持唤醒/不锁定屏幕（充电时屏幕不会休眠）”。
4. 进入“设置 > 电池”，关闭省电模式和超级省电；将目标 APP 与 PCAPdroid 的后台电池策略设为“不限制”。
5. 在最近任务界面锁定目标 APP 和 PCAPdroid（如果厂商系统提供此功能），避免系统自动清理。
6. 保持设备接入 USB 或充电电源，点亮并人工解锁屏幕，再启动 Agent。

- 界面中的“保持唤醒”通常只在设备接入电源时生效，运行期间不要断电。
- Agent 不会绕过 PIN、密码或生物识别锁屏，开始前必须人工解锁。
- 长时间亮屏、录屏和充电会产生热量，应保证散热并观察电池温度。
- 保持 PCAPdroid VPN 处于允许状态，并在厂商任务管理器中锁定 PCAPdroid/目标 APP（如果系统提供该功能）。

任务结束后，回到相同的手机设置页面，按任务前记录恢复自动熄屏、自动上锁、保持唤醒、省电模式和应用电池策略。

### 3.3 Agent 文本输入：ADBKeyboard

AutoGLM、Seed LLM Agent 及其他 Executor 执行 `Type` 动作时，需要手机端安装 ADBKeyboard。当前运行时默认启用 `MINI_PILOT_FORCE_ADB_KEYBOARD=1`；如果系统找不到 `com.android.adbkeyboard/.AdbIME`，文本输入会直接报错，不会静默改用不可靠的中文输入方式。

准备步骤：

1. 获取 `ADBKeyboard.apk`，放到项目根目录或任意已知路径。当前代码提供的下载页为 [senzhk/ADBKeyBoard](https://github.com/senzhk/ADBKeyBoard/blob/master/ADBKeyboard.apk)。
2. 安装并启用输入法：

   ```text
   adb install -r ADBKeyboard.apk
   adb shell ime enable com.android.adbkeyboard/.AdbIME
   ```

3. 验证安装结果：

   ```text
   adb shell pm list packages com.android.adbkeyboard
   adb shell ime list -s
   ```

   输出中应分别出现 `com.android.adbkeyboard` 和 `com.android.adbkeyboard/.AdbIME`。

4. 长时间 Agent 开始前，建议手动将 ADBKeyboard 切换为默认输入法：

   ```text
   adb shell settings get secure default_input_method
   adb shell ime set com.android.adbkeyboard/.AdbIME
   adb shell settings get secure default_input_method
   ```

   先记录第一条命令返回的原输入法；最后一条命令应返回 `com.android.adbkeyboard/.AdbIME`。任务结束后可执行 `adb shell ime set <原输入法ID>` 恢复。

MiniPilot 在每次 `Type` 时也会自动启用并切换 ADBKeyboard，输入结束后恢复进入该动作前的输入法。提前手动设为默认输入法可以减少部分厂商系统阻止切换或首次授权导致的长任务中断。连接多台设备时，在每条命令的 `adb` 后增加 `-s <设备序列号>`。

### 3.4 抓包方式

#### 无 Root：PCAPdroid

集成采集默认会在启动前验证 PCAPdroid API Key。准备步骤如下：

1. 将项目配套的定制 PCAPdroid APK 放到项目根目录。APK 稍后加入时无需修改打包脚本，根目录 APK 会自动进入发布包。
2. 将下面命令中的文件名替换为根目录中的实际 APK 文件名并安装：

   ```text
   adb install -r <定制PCAPdroid文件名>.apk
   ```

3. 验证安装结果：

   ```text
   adb shell pm list packages com.emanuelef.remote_capture
   ```

   输出应包含 `com.emanuelef.remote_capture` 或 `com.emanuelef.remote_capture.debug`。普通商店版或不含 MiniPilot 控制协议的版本不能替代该定制 APK。
4. 打开 PCAPdroid，进入 `Settings > Control Permissions`。
5. 生成 API Key。
6. 将 Key 写入 `mini_pilot.config.json` 的 `capture.pcapdroid_api_key`。

这里必须使用单个字符串字段 `pcapdroid_api_key`，不要写成 `pcapdroid_api_keys` 对象。该 Key 来自手机端定制 PCAPdroid，与模型平台的 API Key 无关。更换手机或在 PCAPdroid 中重新生成 Key 后，应同步更新配置。旧版 PCAPdroid 不支持预检时，应优先升级；只有明确接受兼容风险时才使用 `--capture-skip-pcapdroid-preflight`。

#### Root：tcpdump

Root 模式要求：

1. `adb shell su -c id` 能返回 Root 身份。
2. 手机上可执行 `tcpdump`，位置可以是 `/system/bin/tcpdump`、`/system/xbin/tcpdump` 或 `/data/local/tmp/tcpdump`。
3. 若设备上没有 tcpdump，需要自行推送并赋予执行权限；采集器不会自动安装它。

`auto` 模式仅在同时检测到 Root 和 tcpdump 时选择 Root，否则转为无 Root 模式。

## 4. Python 依赖核对

根目录 `requirements.txt` 是集成运行的唯一依赖入口，已覆盖当前代码中的所有强制第三方导入：

| 包 | 实际用途 |
|---|---|
| `Pillow` | Agent 截图处理、图像统计和模型输入。 |
| `openai` | 调用 OpenAI 兼容模型接口。 |
| `PyYAML` | 采集配置和样本 YAML 的生成与读取。 |
| `opencv-python` | 实时/离线 QoE、视频读取和画面分析。 |
| `numpy` | 图像和时序计算。 |
| `scapy` | PCAP 解析和 SA CSV 生成。 |
| `rapidocr-onnxruntime` | 离线 QoE 的 OCR。 |

`adb`、scrcpy、FFmpeg、tcpdump、ADBKeyboard、定制 PCAPdroid 和 Tk 不是 pip 包，因此不能仅靠 `requirements.txt` 安装。`pytesseract` 是可选增强，缺失时实时 QoE 会自动关闭对应 OCR 路径。

安装命令：

```bash
python -m venv .venv
```

Windows PowerShell：

```powershell
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

macOS/Linux：

```bash
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

不要复制或分发 `.venv`。虚拟环境含本机绝对路径和平台相关二进制，接收方应根据 `requirements.txt` 重新创建。

## 5. 配置文件

### 5.1 创建配置

Windows PowerShell：

```powershell
Copy-Item mini_pilot.config.example.json mini_pilot.config.json
```

macOS/Linux：

```bash
cp mini_pilot.config.example.json mini_pilot.config.json
```

`mini_pilot.config.json` 是本地配置，可能包含模型和 PCAPdroid 密钥，已被 `.gitignore` 排除，不应发给其他人。发布包只包含 `mini_pilot.config.example.json`。

### 5.2 最小可用配置

```json
{
  "runs_dir": "runs",
  "runtime": {
    "max_loops": 100
  },
  "executor": {
    "active_profile": "autoglm",
    "profiles": {
      "autoglm": {
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "model": "autoglm-phone",
        "api_key": "YOUR_ZHIPU_API_KEY",
        "temperature": 0.0,
        "max_tokens": 1024,
        "image_max_side": 2048
      },
      "seed2pro": {
        "base_url": "https://ark.cn-beijing.volces.com/api/v3",
        "model": "doubao-seed-2-0-pro-260215",
        "api_key": "YOUR_ARK_API_KEY",
        "temperature": 0.0,
        "max_tokens": 1024,
        "image_max_side": 2048
      }
    }
  },
  "capture": {
    "enabled": true,
    "backend": "unicapture",
    "operation_mode": "agent",
    "business_start_timeout": 100,
    "activation_fallback_delay": 30,
    "mode": "noroot",
    "pcap_mode": "standard",
    "enable_pcap": true,
    "pcapdroid_preflight": true,
    "enable_qoe": true,
    "enable_postprocess": true,
    "enable_flow_labeling": true,
    "postprocess_enable_vlm": true,
    "postprocess_vlm_base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
    "postprocess_vlm_model": "qwen-vl-plus",
    "postprocess_vlm_api_key": "YOUR_DASHSCOPE_API_KEY",
    "postprocess_vlm_every": 5,
    "postprocess_vlm_max_calls": 20,
    "pcapdroid_api_key": "YOUR_PCAPDROID_API_KEY"
  }
}
```

示例同时保留两个可用模型档案，但一次任务只会使用 `active_profile` 指定的一个。使用 AutoGLM 时保留 `"active_profile": "autoglm"`；使用 Seed 2.0 Pro 时改为 `"active_profile": "seed2pro"`。未使用档案中的占位 Key 不影响任务启动，但执行 `python model_ping.py` 时会检查所有档案，因此应使用 `--target` 只验证当前档案。

### 5.3 模型公共字段

| 字段 | 必填 | 填写内容 |
|---|---|---|
| `executor.active_profile` | 是 | 要启用的档案名，必须与 `executor.profiles` 下的键完全一致。 |
| `base_url` | 是 | OpenAI 兼容接口的基础地址。不同平台路径不同，必须使用下文给出的完整值。 |
| `model` | 是 | 服务端实际提供的视觉模型 ID，不是自定义备注名。 |
| `api_key` | 是 | 对应模型平台创建的 ASCII API Key，不是登录密码，也不是 PCAPdroid Key。 |
| `temperature` | 否 | 建议 `0.0`，使手机动作更稳定。 |
| `max_tokens` | 否 | 单次模型响应上限，默认建议 `1024`。 |
| `image_max_side` | 否 | 发送给模型前的截图最长边，默认建议 `2048`。 |
| `extra_body` | 否 | 服务商要求的额外 JSON 请求字段。 |

模型配置优先级为：命令行模型参数 > 选中的配置档案 > `MINI_PILOT_*` 环境变量 > 代码默认值。MiniPilot 当前通过 OpenAI 兼容的 Chat Completions 接口发送文字和手机截图，所以选择其他模型时也必须确认其接口和模型均支持图像输入。

### 5.4 AutoGLM-Phone（智谱开放平台）

1. 注册并登录[智谱 AI 开放平台](https://open.bigmodel.cn/)。
2. 进入控制台的 [API Keys](https://open.bigmodel.cn/usercenter/proj-mgmt/apikeys)，创建或复制 API Key。官方鉴权说明见[智谱 API 快速开始](https://docs.bigmodel.cn/cn/api/introduction)。
3. 确认账户具备 `AutoGLM-Phone` 的调用权限和可用额度。模型说明及官方接入示例见[AutoGLM-Phone 文档](https://docs.bigmodel.cn/cn/guide/models/vlm/autoglm-phone)。
4. 在 `executor.profiles.autoglm` 中填写：

   ```json
   "autoglm": {
     "base_url": "https://open.bigmodel.cn/api/paas/v4",
     "model": "autoglm-phone",
     "api_key": "在智谱开放平台创建的API Key",
     "temperature": 0.0,
     "max_tokens": 1024,
     "image_max_side": 2048
   }
   ```

5. 将 `executor.active_profile` 设为 `autoglm`。`model` 使用接口实际 ID `autoglm-phone`，不要填展示名称 `AutoGLM-Phone`，也不要填本项目代码中的本地默认模型名。

### 5.5 Seed LLM Agent / 豆包 Seed 2.0 Pro（火山方舟）

1. 注册并登录[火山引擎](https://console.volcengine.com/)，进入火山方舟控制台。
2. 在[方舟 API Key 管理](https://console.volcengine.com/ark/region:ark+cn-beijing/apikey)页面创建 API Key。官方管理说明见[管理 API Key](https://www.volcengine.com/docs/82379/1361424)。
3. 在方舟控制台确认豆包 Seed 2.0 Pro 已对当前账户和北京地域开放，并确认账户有可用额度。方舟的模型版本可能更新；本项目当前配置使用 `doubao-seed-2-0-pro-260215`，如果控制台给出的可调用模型 ID 不同，应以控制台当前模型列表为准。
4. 在 `executor.profiles.seed2pro` 中填写：

   ```json
   "seed2pro": {
     "base_url": "https://ark.cn-beijing.volces.com/api/v3",
     "model": "doubao-seed-2-0-pro-260215",
     "api_key": "在火山方舟创建的API Key",
     "temperature": 0.0,
     "max_tokens": 1024,
     "image_max_side": 2048
   }
   ```

5. 将 `executor.active_profile` 设为 `seed2pro`。这里使用的是火山方舟按量调用地址；官方 OpenAI SDK 示例和基础地址见[方舟开始使用](https://www.volcengine.com/docs/82379/1795150)。不要误填 Coding Plan 的专用地址或其他地域地址。

### 5.6 模型切换与验证

只验证 AutoGLM：

```bash
python model_ping.py --target autoglm
```

只验证 Seed 2.0 Pro：

```bash
python model_ping.py --target seed2pro
```

看到 `SUCCESS` 才表示模型地址、模型 ID、API Key 和账户权限已经共同通过检查。`401` 通常表示 Key 错误或失效；`403` 通常表示权限或额度问题；`404`/模型不存在通常表示 `base_url` 或 `model` 填错。连通性检查成功后，可用 `active_profile` 选择默认模型，也可在单次命令中用 `--profile autoglm` 或 `--profile seed2pro` 临时覆盖。

同时验证配置中的全部模型：

```bash
python model_ping.py
```

### 5.7 后处理 VLM：Qwen-VL（阿里云百炼）

后处理 VLM 与 Executor 是两套独立配置：Executor 负责操作手机，后处理 VLM 负责识别录屏或实时截图中的业务开始状态。当前项目使用阿里云百炼（Model Studio）提供的 `qwen-vl-plus`。标准采集配置启用 `postprocess_enable_vlm`，因此以下三个字段都是必填项，不能依赖 Executor 档案回退：

| 字段 | 当前填写值 | 含义 |
|---|---|---|
| `capture.postprocess_vlm_base_url` | `https://dashscope.aliyuncs.com/compatible-mode/v1` | 百炼华北 2（北京）的 OpenAI 兼容基础地址。不要在末尾追加 `/chat/completions`。 |
| `capture.postprocess_vlm_model` | `qwen-vl-plus` | 当前项目使用的 Qwen-VL 模型 ID，必须保持小写和连字符。 |
| `capture.postprocess_vlm_api_key` | `YOUR_DASHSCOPE_API_KEY` | 在阿里云百炼创建的 API Key，实际使用时替换整个占位符。 |

完整配置如下：

```json
"postprocess_enable_vlm": true,
"postprocess_vlm_base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
"postprocess_vlm_model": "qwen-vl-plus",
"postprocess_vlm_api_key": "在阿里云百炼创建的API Key",
"postprocess_vlm_every": 5,
"postprocess_vlm_max_calls": 20
```

获取提供商和 API Key：

1. 提供商是**阿里云百炼（Model Studio）**，产品内的模型系列是**通义千问 Qwen-VL**；不是 ModelScope，也不是智谱或火山方舟。登录[阿里云百炼控制台](https://bailian.console.aliyun.com/)，首次使用时按页面提示开通服务。
2. 在控制台右上角选择“华北 2（北京）”，进入“API Key”页面并单击“创建 API Key”。建议选择默认业务空间；权限选“全部”，或者在自定义模型权限中明确加入 `qwen-vl-plus`。详细步骤见[阿里云百炼：获取 API Key](https://help.aliyun.com/zh/model-studio/get-api-key)。
3. 创建成功时立即复制 API Key。新 Key 的完整明文通常只展示一次；这里要填的是百炼 API Key，不是阿里云 AccessKey ID/AccessKey Secret。
4. 同时记录创建弹窗给出的 API Host。当前项目现有账号使用 `https://dashscope.aliyuncs.com/compatible-mode/v1`；该地址目前仍可用。新创建的业务空间可能显示 `https://<业务空间ID>.cn-beijing.maas.aliyuncs.com/compatible-mode/v1`，此时应将弹窗中的完整 API Host 原样填入 `postprocess_vlm_base_url`，并确保 Key、业务空间和地域一致。官方 Qwen-VL 接口说明见[OpenAI 兼容视觉模型调用](https://help.aliyun.com/zh/model-studio/qwen-vl-compatible-with-openai)。
5. `postprocess_vlm_model` 当前固定填 `qwen-vl-plus`。如果未来在百炼控制台切换模型，必须选择支持图像输入及 OpenAI Chat Completions 的 Qwen-VL 模型，并同步填写控制台展示的准确模型 ID。

单独验证 Qwen-VL 配置：

```bash
python model_ping.py --target postprocess_vlm
```

必须看到 `capture.postprocess_vlm` 的 `SUCCESS`。如果显示 `401`，检查 Key 是否复制完整；如果显示 `403`，检查 Key 的业务空间、地域和模型权限；如果显示 `404`，检查 API Host 和模型 ID。不要把 `YOUR_DASHSCOPE_API_KEY`、中文说明文字或其他平台的 Key 留在配置中。

### 5.8 采集字段

| 字段 | 推荐值 | 含义 |
|---|---|---|
| `capture.enabled` | `true` | 未显式传 `--capture` 时是否自动启用采集。 |
| `backend` | `unicapture` | 当前唯一支持的采集后端。 |
| `operation_mode` | `agent` | `agent` 为模型操作，`manual` 为人工操作。 |
| `business_start_timeout` | `100` | 等待目标业务真正开始的最大额外秒数。 |
| `activation_fallback_delay` | `30` | 检测到前台/日志激活后，继续等待 VLM 确认的秒数。 |
| `mode` | `noroot` | `auto`、`root` 或 `noroot`。无 Root 设备建议明确填 `noroot`。 |
| `pcap_mode` | `standard` | `minimal` 截取 64 字节且仅保留常用业务端口；`standard` 截取 96 字节并过滤部分非业务流量；`full` 不截断。 |
| `enable_pcap` | `true` | 是否抓取网络流量。关闭后不要求 PCAPdroid/tcpdump。 |
| `pcapdroid_preflight` | `true` | 启动前验证 PCAPdroid 安装、协议和 Key。 |
| `pcapdroid_api_key` | PCAPdroid 中生成的 Key | 单个字符串。来自手机端 `Settings > Control Permissions`，不是任何模型平台的 Key。 |
| `enable_qoe` | `true` | 是否启动实时 QoE 监测。 |
| `enable_postprocess` | `true` | 是否在原始文件落盘后自动执行后处理。 |
| `enable_flow_labeling` | `true` | 是否在原始 SA CSV 旁新建 `*_flow_labeled.csv`；不会覆盖原文件或已有标注文件。 |
| `postprocess_enable_vlm` | `true` | 是否用视觉模型辅助判断业务开始；标准采集必须启用并填写上节三个 VLM 字段。 |
| `postprocess_vlm_base_url` | 百炼 API Host | 必填；当前账号可用 `https://dashscope.aliyuncs.com/compatible-mode/v1`。 |
| `postprocess_vlm_model` | `qwen-vl-plus` | 必填；当前后处理视觉模型 ID。 |
| `postprocess_vlm_api_key` | 百炼 API Key | 必填；必须与 API Host 属于同一地域和业务空间。 |
| `postprocess_vlm_every` | `5` | 粗定位时每隔多少秒调用一次 VLM。 |
| `postprocess_vlm_max_calls` | `20` | 单个样本最多调用 VLM 的次数。 |

即使 Executor 使用 AutoGLM 或 Seed 2.0 Pro，以上 Qwen-VL 三项也必须单独填写。三个 Key 的用途分别为：Executor 平台 Key 用于手机操作，百炼 Key 用于业务画面识别，PCAPdroid Key 用于控制手机端抓包；三者不能混用。

还可在 `capture` 中固定 `app`、`scene`、`resolution`、`location`、`pcap_app_package`、`unicapture_dir` 或 `python`。一般建议在每次命令或 GUI 中选择 APP 和场景，避免配置长期固定后采错对象。

运行目录使用 `runs_dir`。当前实现中，如果配置文件提供了 `runs_dir`，它优先于命令行 `--runs-dir`；若希望命令行决定目录，请从配置中删除该字段。

## 6. 配置 APP 和场景

当前内置 APP、包名和场景来自 `unicapture/app_configs.py`。推荐通过 GUI 的 Category、App、Scene 下拉框选择，避免手写错误。

命令行查看内置 APP。每个 APP 后会同时显示可填写的场景名：

```bash
python -m mini_pilot.main --list-apps
```

查看某个 APP 的场景说明和默认采集时长：

```bash
python -m mini_pilot.main --list-scenes douyin
```

直接使用 Unicapture 时，等价命令如下；查询场景不需要填写 `--scene`：

```bash
python unicapture/app_collector.py --list-apps
python unicapture/app_collector.py --app-name douyin --list-scenes
```

命令行运行时，`--capture-app` 和 `--capture-scene` 必须使用内部英文标识，例如：

```text
douyin + feed
kuaishou + feed
mango + movie
genshin + gameplay
tencent_meeting + audio_meeting
```

采集未内置的 APP 时，显式提供名称、包名、类型和场景：

```text
python -m mini_pilot.main --goal "打开示例应用并浏览 3 分钟" --run-duration 180 --capture unicapture --capture-app example_app --capture-scene feed --custom-app-package com.example.app --custom-app-type video --custom-app-scene feed
```

APP 类型会影响最终 QoE 字段：手游主要生成 RTT；视频、直播、会议、音视频通话、云手机等主要生成分辨率和卡顿；不适用的指标写为 `-4`，适用但无法可靠识别的指标写为 `-128`。

## 7. 完整操作流程

### 7.1 启动前检查

1. 激活 `.venv`。
2. 确认 `mini_pilot.config.json` 已填写模型和当前设备的 PCAPdroid Key。
3. 执行 `python model_ping.py`。
4. 执行 `adb devices`，确认目标设备状态为 `device`。
5. 在手机设置界面确认开发者选项中的“保持唤醒”、USB 调试安全设置、最长自动锁屏时间和“不限制”电池策略已经配置。
6. 执行 `adb shell ime list -s`，确认存在 `com.android.adbkeyboard/.AdbIME`，再执行 `adb shell ime set com.android.adbkeyboard/.AdbIME` 将其设为默认输入法。
7. 确认目标 APP 已安装、已登录，并处于允许采集的账号和网络环境。
8. 无 Root 模式下确认安装的是项目配套的定制 PCAPdroid，并核对 Control Permissions 中的 Key。
9. 长于 180 秒的任务执行 `scrcpy --version`；需要完整后处理时执行 `ffmpeg -version` 和 `ffprobe -version`。

### 7.2 GUI：Agent 自动操作

```bash
python -m mini_pilot.gui
```

在窗口中依次：

1. 在 `Device` 选择设备。
2. 在 `Goal` 输入自然语言目标。
3. 在 `Duration` 填写有效业务时长，单位为秒。
4. `Capture` 选择 `unicapture`。
5. 选择 Category、App 和 Scene。
6. 点击 `Start`。
7. 运行中可在 Live Command 输入补充要求：`Send` 持续生效，`Once` 只执行一次，`Clear` 清空补充要求，`Stop` 停止任务。

GUI 当前运行 Agent 模式；需要人工操作时使用命令行 `--manual`。

### 7.3 命令行：Agent 自动操作

```text
python -m mini_pilot.main --goal "打开抖音，浏览推荐短视频 5 分钟" --run-duration 300 --capture unicapture --capture-app douyin --capture-scene feed --device-id YOUR_ADB_DEVICE_SERIAL
```

`--run-duration` 表示业务开始后的有效时长。采集进程的上限会自动增加 `business_start_timeout`，避免把启动和加载时间算进有效业务时长。

### 7.4 命令行：人工操作

```text
python -m mini_pilot.main --manual --goal "人工操作抖音推荐流 5 分钟" --run-duration 300 --capture unicapture --capture-app douyin --capture-scene feed --device-id YOUR_ADB_DEVICE_SERIAL
```

终端显示 Unicapture 就绪后，直接在手机上操作。倒计时在检测到目标业务开始后启动。按 `Ctrl+C` 可以提前结束并触发文件收集。

### 7.5 不采集，仅运行 Agent

```bash
python -m mini_pilot.main --capture none --goal "打开设置并查看网络状态"
```

### 7.6 重复采集

```text
python -m mini_pilot.main --goal "打开快手浏览推荐流 3 分钟" --run-duration 180 --capture unicapture --capture-app kuaishou --capture-scene feed --repeat-count 5 --repeat-delay 10
```

每轮创建独立运行目录。任一轮失败后，当前命令停止继续重复。

## 8. 采集生命周期

一次集成采集按以下顺序执行：

1. 解析配置，选择设备、APP、场景和操作模式。
2. 创建独立 `runs/<时间戳>-<任务标识>/`。
3. 无 Root 模式执行 PCAPdroid API Key 预检。
4. 启动 Unicapture：logcat、抓包、录屏和实时 QoE。
5. Unicapture 写入 ready 文件，MiniPilot 开始 Agent 或人工操作。
6. 检测目标 APP 前台/日志激活，并可由 VLM 确认实际业务开始。
7. 达到有效业务时长、任务完成、收到 Stop 或发生错误后停止操作。
8. 先完成录屏，再停止 QoE、抓包和 logcat。
9. 拉取设备文件并生成 YAML、SA CSV、QoE CSV。
10. 执行业务时间、QoE 和流级标注后处理；失败时保留全部原始文件。
11. 默认 force-stop 目标 APP；使用 `--no-close-app` 可关闭此行为。

## 9. 输出结构

```text
runs/<时间戳>-<任务标识>/
|- input/
|  |- goal.txt
|  |- live_commands.txt
|  `- live_commands.queue.jsonl
|- execution/
|  |- summary.json
|  `- screenshots/
`- capture/
   |- unicapture_command.txt
   |- unicapture_stdout.log
   |- business_timing.json
   `- <app>-<时长>-<时间戳>/
      |- cut_<prefix>.pcap 或 .pcapng
      |- <prefix>.mp4
      |- <prefix>.log
      |- <prefix>.yaml
      |- <prefix>_sa.csv
      |- <prefix>_qoe.csv
      `- postprocess/
         |- summary.json
         |- activate_time_report/
         |- activate_time_backups/
         `- qoe_backups/
```

最终 QoE CSV 固定字段为：

```text
file_name,time,rtt,trust_resolution,trust_stall,loading_reason
```

PCAP、MP4 或日志可能因权限、外部工具或设备能力缺失而不存在。应首先查看 `capture/unicapture_stdout.log` 和样本目录下的 `postprocess/summary.json`，不要只根据主进程退出码判断样本是否完整。

## 10. 常见问题

### 找不到设备

- 执行 `adb kill-server`、`adb start-server` 和 `adb devices`。
- 确认手机已接受当前电脑的调试授权。
- 多设备时显式传 `--device-id`。

### PCAPdroid Key 未配置或错误

- 确认 `mini_pilot.config.json` 使用单值字段 `capture.pcapdroid_api_key`，不要填写设备序列号或使用 `pcapdroid_api_keys` 映射。
- 重新从 PCAPdroid `Settings > Control Permissions` 复制当前 Key。
- 确认安装的是支持预检协议的定制版本。
- 若不需要网络流量，显式使用 `--capture-no-pcap`，不要伪造 Key。

### 录屏在 180 秒附近结束

- 当前环境没有找到可执行的 scrcpy，程序回退到了 `screenrecord`。
- 安装 scrcpy 并加入 PATH，或将正确的可执行文件放到 `unicapture/scrcpy/`。

### 后处理显示 `partial_error`

- 查看样本目录下 `postprocess/summary.json` 的 `activate_time` 和 `qoe` 子项。
- 确认 MP4 可解码、FFmpeg 可用、模型配置正确。
- 后处理失败不会删除 PCAP、MP4、LOG、原始 YAML 或原始 SA CSV。

### 模型能连接但不会正确操作

- 模型必须支持图像输入和 OpenAI 兼容的 Chat Completions 请求。
- `model` 必须填写服务端真实模型 ID。
- 保持 `temperature` 为 `0.0`，并先用短任务验证。

## 11. 发布打包

在项目根目录执行：

```bash
python package_project.py
```

默认输出到 `dist/minipilot-<时间戳>.zip`。脚本会自动排除：

- `.git`、`.svn` 等版本控制目录。
- `.venv`、`venv`、`env` 等虚拟环境。
- `runs`、采集数据、日志、PCAP、录屏和缓存。
- `mini_pilot.config.json`、配置副本、`.env`、证书和密钥。
- IDE 文件、系统元数据、旧压缩包和备份文件。

发布包保留源代码、三份用户文档、配置模板、统一依赖清单及运行所需的仓库内工具。接收方解压后必须重新创建虚拟环境，并从配置模板生成自己的本地配置。
