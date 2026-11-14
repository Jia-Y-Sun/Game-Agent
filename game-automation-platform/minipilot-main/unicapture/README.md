# 安卓端侧不同领域APP样本采集工具

基于已有数据样本格式和采集方法，提供完整的安卓端侧APP样本采集方案。

## 文件说明

| 文件 | 说明 | 是否必需 |
|-----|------|---------|
| `安卓端侧APP样本采集方案.md` | 完整采集方案文档 | 是 |
| `极简操作手册.md` | **快速上手指南（新人先看这个）** | 是 |
| `app_configs.py` | 不同领域APP配置模板 | 是 |
| `app_collector.py` | 主采集脚本，支持单个APP采集 | 是 |
| `batch_collector.py` | 批量采集脚本，支持多APP多场景批量执行 | 否 |
| `smart_collector.py` | 自然语言/简化入口采集脚本 | 否 |
| `generate_metadata.py` | 生成元数据标注文件（yaml/csv） | 是（通常由主脚本自动调用） |
| `qoe_monitor.py` | QoE自动监测脚本（卡顿/分辨率/FPS） | 否 |
| `check_large_files.py` | 手机大文件清理工具 | 否 |
| `requirements.txt` | Python依赖列表 | 是 |
| `tcpdump` | Android ARM64 抓包二进制（root模式使用） | root模式必需 |
| `数据样本样例/` | 标准输出格式样例 | 参考用 |

## 快速开始

### 1. 环境准备

- Python 3.8+
- ADB 工具（Android Debug Bridge）
- 一台 Android 手机，开启 USB 调试
- Windows 推荐使用 PowerShell，Linux/Mac 使用 Bash

### 2. 安装依赖

```bash
# 建议使用虚拟环境
python -m venv .venv
source .venv/bin/activate  # Linux/Mac
# .venv\Scripts\activate   # Windows

pip install -r requirements.txt
```

> **注意**：`pytesseract` 仅在使用 OCR 分辨率识别时才需要。Windows 用户还需额外安装 [Tesseract-OCR 引擎](https://github.com/UB-Mannheim/tesseract/wiki)。

### 3. 连接Android设备

```bash
# 检查设备连接
adb devices

# 确保USB调试已开启
```

### 4. 单次采集

```bash
# 采集芒果TV电影播放场景（自动模式：优先root，无root则用PCAPdroid）
python app_collector.py --app-name mango --scene movie --duration 600

# 无Root模式采集（需提前安装PCAPdroid等VpnService抓包App）
python app_collector.py --app-name mango --scene movie --duration 600 --capture-mode noroot

# 采集王者荣耀对战场景
python app_collector.py --app-name honorofkings --scene battle --device abc123

# 微信视频通话采集
python app_collector.py --app-name wechat --scene video_call --duration 300
```

### 5. 批量采集

```bash
# 创建示例配置文件
python batch_collector.py --create-sample

# 编辑 batch_config_sample.yaml 后执行
python batch_collector.py --config batch_config_sample.yaml

# 批量采集使用无root模式
python batch_collector.py --config batch_config_sample.yaml --capture-mode noroot
```

### 6. 自然语言/简化采集

```bash
# 使用自然语言描述采集任务
python smart_collector.py "采集60秒芒果TV电影视频"

# 或指定参数
python smart_collector.py --app-name mango --scene movie --duration 60
```

### 7. QoE监测

```bash
# 启动QoE监测（卡顿/分辨率/FPS）
python qoe_monitor.py --device abc123 --interval 1.0
```

### 8. 手机大文件清理

```bash
# 扫描手机中大于50MB的文件
python check_large_files.py

# 自动清理已知临时文件（screenrecord、pcap等）
python check_large_files.py --auto-clean-temp
```

## 查看支持的APP和场景

```bash
# 列出所有支持的APP
python app_collector.py --list-apps

# 查看指定APP的所有场景（不需要填写 --scene）
python app_collector.py --app-name mango --list-scenes

# 查看APP详细信息
python app_configs.py
```

## 采集数据格式

采集完成后，会生成以下文件（六种标准格式）：

```
data/
├── {prefix}.yaml          # 环境信息和元数据
├── {prefix}_qoe.csv       # QoE指标（卡顿/分辨率/时延）
├── {prefix}_sa.csv        # SA流标注
├── {prefix}.mp4           # 屏幕录制视频
├── {prefix}.log           # 系统日志
└── cut_{prefix}.pcap      # 流量报文（root/tcpdump 或 无root/PCAPdroid）
```

文件名格式：
```
{应用名}_{设备ID}_{场景}_{分辨率}_{时长}_{时间戳}

示例：mango_b29154b4_movie_liuchang360P_600_20260318T221434
```

## 配置说明

### APP配置 (app_configs.py)

每个APP包含以下配置：
- `name`: 应用名称
- `app_type`: 应用类型（video/game/social/shopping/cloudgame）
- `package`: 应用包名
- `scenes`: 支持的采集场景列表
- `qoe_metrics`: 支持的QoE指标

### 批量采集配置 (batch_config.yaml)

```yaml
devices:
  - id: b29154b4
    brand: xiaomi
    model: redmi k80
    location: lab_shanghai

apps:
  - name: mango
    scenes:
      - name: movie
        resolutions: ["流畅360P", "高清720P"]
        duration: 600

schedule:
  repeat: 3      # 每个场景重复次数
  interval: 60   # 任务间隔(秒)

output_dir: ./data
```

## 无Root采集说明

本工具支持在无Root权限的设备上进行抓包采集，核心原理是利用 Android `VpnService` API（与 PCAPdroid 等工具相同的技术）。

### 前置条件

1. **安装无Root抓包App**（以下任选其一）
   - **PCAPdroid（推荐）**: [GitHub Releases](https://github.com/emanuele-f/PCAPdroid/releases) 或 Google Play
   - HttpCanary / 小黄鸟
   - 其他基于 VpnService 的抓包工具

2. **授予VPN权限**
   - 首次使用时，抓包App会请求"建立VPN连接"权限，请点击"允许"

### 无Root采集流程

```bash
# 方式1: 强制无root模式
python app_collector.py --app-name mango --scene movie --capture-mode noroot

# 方式2: 自动模式（无root设备自动fallback到PCAPdroid）
python app_collector.py --app-name mango --scene movie --capture-mode auto

# 方式3: 指定自定义抓包App包名
python app_collector.py --app-name mango --scene movie --capture-mode noroot --pcap-app-package com.xxx.xxx
```

### 工作流程

1. 脚本自动检测已安装的无root抓包App（PCAPdroid等）
2. 通过 `adb` 启动抓包App
3. **用户在手机上点击"开始捕获"**（启动VPN抓包）
4. 脚本执行拨测任务（启动目标App、等待指定时长）
5. **用户在手机上点击"停止"并保存文件**
6. 脚本自动搜索设备上的pcap文件并拉取到PC

### 注意事项

- 无root模式下，录屏、日志获取均不需要root权限
- PCAPdroid 默认保存路径通常为 `/sdcard/Download/PCAPdroid/`
- 建议在PCAPdroid设置中将导出格式设为 **PCAP**（而非PCAPNG），兼容性更好
- 无root抓包仅捕获**出站连接**，且IP/TCP头部为合成数据（与PCAPdroid非root模式的限制一致）

### Root vs 无Root 对比

| 特性 | Root模式 (tcpdump) | 无Root模式 (VpnService) |
|------|-------------------|------------------------|
| 所需权限 | 需Root + tcpdump | 无需Root，需安装抓包App |
| 包完整性 | 100%原始数据 | L3/L4头部合成，L7数据完整 |
| 入站流量 | 可捕获 | 不可捕获 |
| 包大小/时序 | 精确 | 可能与原始有差异 |
| 操作复杂度 | 全自动 | 需用户在手机上点2次按钮 |
| 适用场景 | 自动化批量采集 | 普通未Root手机采集 |

## 扩展新的APP

在 `app_configs.py` 中添加新的APP配置：

```python
NEW_APPS = {
    "new_app": AppConfig(
        name="new_app",
        app_type="video",
        package="com.example.newapp",
        scenes=[
            SceneConfig(
                name="playback",
                description="播放视频",
                duration=600,
                params={"channel": "推荐"}
            ),
        ],
        qoe_metrics=["trust_stall", "trust_resolution"]
    ),
}
```

## 交付与分发说明

### 交付时应排除的文件/目录

以下文件/目录为本地开发/运行产生，交付给他人时请勿打包：

```
__pycache__/
.idea/
.venv/
.venv313/
*-beifen.py
*.log
```

### 核心交付文件清单

```
unicapture/
├── app_configs.py
├── app_collector.py
├── batch_collector.py
├── generate_metadata.py
├── qoe_monitor.py
├── qoe_postprocess/      # 录屏/VLM 秒级 QoE 后处理
├── postprocess/          # logcat 应用前台时间标注
├── postprocess_runner.py # 采集完成后的统一自动入口
├── smart_collector.py
├── check_large_files.py
├── requirements.txt
├── tcpdump              # root模式必需
├── README.md
├── 极简操作手册.md
├── 安卓端侧APP样本采集方案.md
├── 样本示例说明.txt
├── .gitignore
├── data/                # 空目录
├── test_data/           # 空目录
├── runs/                # 空目录
└── 数据样本样例/         # 可选：样例数据集
```

## 采集完成后的自动标注

`app_collector.py` 默认在元数据生成完成后自动运行 logcat、VLM 和录屏
后处理。最终输出仍严格使用数据集规定的 8 个 QoE 字段。应用前台时间写入
YAML `business_record`，不会和实际业务画面开始时间混为一个字段。

直接运行采集器时可关闭：

```bash
python app_collector.py ... --disable-postprocess
python app_collector.py ... --postprocess-disable-vlm
```

## 常见问题

### 1. 设备连接失败
- 确保USB调试已开启
- 检查adb驱动是否安装正确
- 尝试重新插拔USB线

### 2. 无法抓取pcap（Root模式）
- 抓取pcap需要root权限 + tcpdump
- 可通过 `adb push tcpdump /data/local/tmp/` 安装tcpdump

### 3. 无法抓取pcap（无Root模式）
- 确保已安装PCAPdroid或其他VpnService抓包App
- 确保已授予抓包App的VPN权限
- 检查脚本是否正确检测到App包名：`adb shell pm list packages | findstr pcapdroid`
- 可在采集前手动打开抓包App测试能否正常工作

### 4. QoE监测不准确
- 调整卡顿判定阈值 `--threshold 90`
- 确保屏幕亮度适中
- 对于视频APP，建议使用特定的分辨率设置

### 5. scapy 安装说明
- 本项目已支持 **首次使用自动检测并安装 scapy**
- 若自动安装失败，可手动执行：`pip install scapy>=2.5.0`
- 若不需要 pcap 解析/SA 标注功能，可忽略 scapy 相关提示

## 相关工具

- [Android Studio / Platform Tools](https://developer.android.com/studio) - 包含ADB工具
- [Wireshark](https://www.wireshark.org/) - 分析pcap文件
- [uiautomator2](https://github.com/openatx/uiautomator2) - UI自动化测试
- [Poco](https://poco.readthedocs.io/) - 跨平台UI自动化

## 许可证

MIT License
