# 样本重建与检查工具

`rebuild_sample.py` 根据样本目录中的 MP4 和 PCAP 重建 YAML 与 QoE CSV，并检查时间边界、文件对应关系及业务时长。

## 使用方法

只检查，不修改：

```powershell
python tools/rebuild_sample.py "D:\minipilot\runs\20260828-163104-task-af3a62cc" --check-only
```

备份并重建：

```powershell
python tools/rebuild_sample.py "D:\minipilot\runs\20260828-163104-task-af3a62cc"
```

输入也可以是具体样本目录。若一个 run 中包含多个样本，必须显式添加 `--all`：

```powershell
python tools/rebuild_sample.py "D:\minipilot\runs\某次任务" --all
```

其他选项：

- `--target-duration 300`：覆盖 YAML 中的目标业务时长。
- `--enable-vlm`：缺少可信业务起点时允许调用配置好的 VLM。
- `--ocr-every 1`：分辨率检测间隔；默认 1 秒，与在线采集一致。

## 安全与输出

- 每个样本必须且只能识别到一个 MP4 和一个主 PCAP；文件歧义时拒绝修改。
- 原 YAML 和 QoE 会备份到样本目录的 `repair/<时间>/before/`。
- 最新检查报告写入输入目录的 `repair/latest_report.json`。
- 视频时长、宽高和帧率来自 `ffprobe`。
- 旧 QoE 只备份，不参与识别。MP4 按 1 秒抽帧并直接复放采集程序的 `PageClassifier`、`ImageAnalyzer`、`LoadingDetector` 与分辨率检测器。
- PCAP 原始包时间会保留；若它与采集墙钟不一致，不会盲目覆盖视频时间轴。
- `activate_record` 和 `business_record` 会限制在视频边界内。
- QoE 的 `file_name` 必须与实际 PCAP 文件名一致。
- 业务不足目标秒数时返回 `warning`，不会伪造缺失的数据。

退出码：`0` 表示检查通过或仅有警告，`2` 表示存在结构错误或无法安全重建。
