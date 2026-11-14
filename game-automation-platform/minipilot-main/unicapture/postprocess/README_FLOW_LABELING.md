# 采集后流级标注

MiniPilot 在本次采集的 `*_sa.csv` 完整落盘后自动执行流级标注，并在同一目录新建：

```text
<sample>_flow_labeled.csv
```

原始 `*_sa.csv` 始终只读；同名标注文件已经存在时，状态记为 `already_exists`，不会覆盖。

## 判定顺序

每条 SA 流按固定优先级处理，首条命中的规则即为结果：

1. `not_in_app=1`：`background`。当前采集器按 `packet_num<=2` 生成该字段，因此它表示小流/噪声的简化基线，并非 UID 级应用归属。
2. `egn_sub_protocol` 以 `DNS` 开头或目标端口为 53：`dns`。
3. 域名命中广告规则：`ad`。
4. 域名命中第三方 SDK 规则：`third_party_sdk`。
5. 在媒体场景中同时满足大流、下行大于上行三倍、包数不少于 10：按场景标为直播、短视频、点播、通话或云游戏媒体流。
6. 其他有有效字节的目标流：`business_api`。
7. 其余流：`unknown`。

每个样本的大流阈值为：

```text
max(flow_bytes 的 75% 分位数, 500000)
```

## 新增字段

- `scene_label`
- `flow_label`
- `party_type`
- `sdk_vendor`
- `confidence`
- `label_reason`
- `large_flow_threshold`
- `source_file`

域名规则保存在 `flow_rules.json`。当前 MiniPilot 的 `domain` 主要来自 PCAP 中的传统 DNS A 记录关联和 IP 前缀映射，尚未直接解析 TLS SNI，因此广告、SDK和第一方归属可能存在漏标。

## 配置

默认开启：

```json
{
  "capture": {
    "enable_flow_labeling": true
  }
}
```

单次关闭：

```bash
python -m mini_pilot.main ... --capture-disable-flow-labeling
```

直接运行 `unicapture/app_collector.py` 时，可使用 `--disable-flow-labeling`。

执行状态和标签统计写入样本目录的 `postprocess/summary.json`。流级标注异常只会把该阶段记为 `error`，不会删除采集产物或把一次有效采集变成失败样本。
