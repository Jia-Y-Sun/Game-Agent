# MiniPilot 采集后业务时间标注

本模块用于在 MiniPilot 和 Unicapture 完成采集后，根据样本的 logcat 定位目标 App 进入前台和离开前台的时间，并将结果写入对应 YAML 的 `business_record`。

模块只执行采集后的时间标注，不会启动 ADB、录屏或抓包，也不会修改 MP4、logcat、PCAP、QoE、SA 文件以及 YAML 中原有的 `video_record`。

## 1. 文件位置

项目根目录运行入口：

```text
annotate_new_samples.py
```

核心模块、规则配置和本说明位于：

```text
unicapture/postprocess/
├── annotate_business_times.py
├── business_time_annotation_profile.json
└── README_BUSINESS_TIME.md
```

业务时间标注功能与原有采集流程相互独立，不需要修改 MiniPilot 或 Unicapture 的采集代码。

## 2. 支持的样本目录

默认扫描项目根目录下的 `runs`：

```text
runs/<应用>/<运行目录>/capture/<样本目录>/
├── <prefix>.yaml
├── <prefix>.log
├── <prefix>.mp4
└── cut_<prefix>.pcap
```

只有同时具有样本信息、`video_record`、匹配的 logcat 和 MP4 的 YAML 才作为完整样本处理。

- 缺少 logcat 或 MP4：标记为 `incomplete_sample`，列入异常清单，不写入 YAML。
- 配置文件或其他非样本 YAML：标记为 `ignored_non_sample_yaml`，直接忽略。
- 已有 `business_record`：默认跳过，不覆盖原有标注。

## 3. 推荐执行流程

必须等待 MiniPilot 采集命令完全结束，并确认 Unicapture 已经生成 YAML、logcat 和 MP4 后，再运行业务时间标注。

### 第一步：试运行

在 `minipilotv1.5` 项目根目录执行：

```bash
python3 annotate_new_samples.py
```

Windows PowerShell 可执行：

```powershell
python .\annotate_new_samples.py
```

试运行只分析样本并生成报告：

- 不修改任何样本 YAML；
- 不生成 YAML 备份；
- 可以提前检查候选时间、置信度和异常样本。

### 第二步：查看报告

报告默认生成在：

```text
runs/business_time_reports/<执行时间>/
├── summary.json
├── business_time_candidates.csv
├── business_time_evidence.csv
└── business_time_exceptions.csv
```

各文件含义：

- `summary.json`：本次扫描数量、状态、置信度、回退情况，以及脚本和规则配置的哈希值。
- `business_time_candidates.csv`：全部被扫描样本的候选开始时间、候选结束时间、事件类型、置信度、写入状态和判定原因。
- `business_time_evidence.csv`：业务边界对应的原始 logcat 行、日志行号、原始时间和校正后的时间，供人工核验。
- `business_time_exceptions.csv`：中置信度、低置信度、不完整样本、无法写入样本和处理错误的待复核清单。

### 第三步：正式写入

确认试运行报告无误后执行：

```bash
python3 annotate_new_samples.py --write
```

正式运行会把符合条件的候选时间写入 YAML。默认配置会处理高、中、低三种置信度，但中低置信度样本仍会保留在待复核清单中。

脚本默认只处理没有 `business_record` 的新样本。已有业务时间标注的样本会在读取大型 logcat 前直接跳过。

## 4. YAML 写入内容

脚本在 YAML 顶层新增以下区块：

```yaml
business_record:
  start_time: 20260707T00350866+0800
  end_time: 20260707T00411215+0800
  start_event: activity_start
  end_event: task_to_back
  source: logcat
  confidence: high
```

时间格式为：

```text
YYYYMMDDTHHMMSSff+ZZZZ
```

其中：

- `YYYYMMDD`：年月日；
- `T`：日期与时间分隔符；
- `HHMMSS`：时、分、秒；
- `ff`：百分之一秒；
- `+0800`：东八区时区。

`video_record` 记录的是完整录屏时间，`business_record` 记录的是根据业务边界规则确定的实际业务时间。两者用途不同，脚本不会用 `business_record` 覆盖或改写 `video_record`。

## 5. 业务边界判定逻辑

业务开始候选主要包括目标 App 的：

- `Activity START`；
- `ActivityResumed` 或 `TopResumedActivity`；
- `Activity Displayed`；
- `Process Start` 等辅助启动信号。

业务结束候选主要包括：

- 目标 App 的 `TaskToBack`；
- 目标 App 离开前台或停止的相关事件；
- 采集控制程序 `CaptureCtrl START`，用于辅助确认目标 App 已退出前台。

对于采集开始时 App 已经驻留后台、日志中没有新的 `Activity START` 的样本，可采用：

```text
目标 App TopResumedActivity + CaptureCtrl START
```

或符合规则的目标 App `TaskToBack` 作为完整业务边界证据。

`Activity Displayed` 可以作为业务开始候选，但采用更严格的时间距离、结束事件配对和最短业务持续时间要求。

## 6. 置信度与时间回退

- `high`：开始、结束事件类型和时间距离满足高置信度规则，并具有足够的日志支撑。
- `medium`：开始和结束候选均存在且顺序合理，但独立佐证不足，或与采集边界的时间距离未达到高置信度要求。
- `low`：缺少部分边界、候选时间异常，或必须复用 `video_record` 时间。

默认配置允许将缺失边界从原有 `video_record` 补齐：

- 仅缺少开始时间：复用 `video_record.start_time`；
- 仅缺少结束时间：复用 `video_record.end_time`；
- 开始和结束均无法定位：复用完整的 `video_record` 时间区间；
- 候选开始时间不早于候选结束时间：改用完整的 `video_record` 时间区间。

使用录屏时间回退不会提高置信度，样本仍标记为低置信度并进入待复核清单。`source` 会记录为：

- `logcat`；
- `logcat+video_record_fallback`；
- `video_record_fallback`。

## 7. YAML 备份

正式写入前，每一个将被修改的 YAML 都会单独备份到：

```text
runs/business_time_backups/<执行时间>/<原相对路径>/<样本>.yaml
```

备份目录保留样本相对于 `runs` 的原始目录结构，因此可以准确找到每个 YAML 对应的修改前版本。

试运行不会创建 `business_time_backups`。正式运行时，只有实际写入的 YAML 才会产生备份。

需要恢复时，应从本次执行时间对应的备份目录中，只复制已经确认的具体 YAML。不要用整个旧备份目录直接覆盖新的 `runs`。

## 8. 常用参数

只分析路径中包含指定文字的样本：

```bash
python3 annotate_new_samples.py --path-contains "搜狐视频"
```

限制本次处理数量：

```bash
python3 annotate_new_samples.py --limit 5
```

处理另一个同格式数据集：

```bash
python3 annotate_new_samples.py --root "/完整的数据集路径"
```

指定数据集时仍应先试运行；确认报告后再增加 `--write`：

```bash
python3 annotate_new_samples.py --root "/完整的数据集路径" --write
```

如需查看全部参数：

```bash
python3 annotate_new_samples.py --help
```

## 9. 安全规则

- 默认是试运行，必须显式增加 `--write` 才会修改 YAML。
- 默认跳过已有 `business_record`，避免重复标注。
- 写入前备份每个原始 YAML。
- 使用临时文件和原子替换完成写入。
- 写入前后逐字校验原有 `video_record` 区块。
- 业务开始时间必须早于业务结束时间。
- MP4、logcat、PCAP、QoE、SA 和截图文件永不修改。
- 缺少必需文件的样本不写入，并进入异常清单。

## 10. 业务时间定位规则配置

适用于 MiniPilot v1.5 样本的业务时间定位规则配置位于：

```text
unicapture/postprocess/business_time_annotation_profile.json
```

该文件仅供业务时间标注模块使用，并不是整个 MiniPilot v1.5 项目的全局配置文件。它包含采集控制程序名称、候选搜索窗口、各种边界允许的时间距离、辅助证据窗口、最短业务持续时间、最低写入置信度和回退策略。

修改该配置只会影响业务时间后处理，不会影响 MiniPilot、ADB 或 Unicapture 的采集行为。修改规则后，应先执行试运行并保留新的报告和配置哈希，再决定是否正式写入。


