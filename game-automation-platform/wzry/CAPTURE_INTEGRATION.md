# 王者与统一采集总控的接口

更新日期：2026-11-09。本文描述代码接口，验证结论以 [状态表](../../docs/IMPLEMENTATION_STATUS.md) 为准。

## 控制顺序

总控启动采集 → 启动游戏 → 启动 external 对局器等待状态 → 前置驱动进入练习场 → 对局器发出 `MATCH_READY` → 总控停止前置驱动并写业务开始标记 → 对局操作 → `MATCH_FINISHED` → 停止游戏输入和采集 → 后处理 → 清单与报告。

前置驱动由 `pregame_driver.py` 承担，使用 ADB 截图、OCR 和开始识别模型。由总控拉起时传入 `--skip-launch-game`，避免重复启动。模型与设备绑定检查在正式启动前执行，详见 [部署说明](README.md)。

## 生命周期

`train.py --mode external` 接受 `--device-id`、`--event-file`、`--stop-file`、`--timeout`、`--match-ready-timeout`、`--run-id`。注意 `--device-id` 是 ADB 设备，旧参数 `--device_id` 是 `cuda:0` 等计算设备，两者不同。

输出事件为 `SCRIPT_STARTED`、`DEVICE_READY`、`WAITING_MATCH_READY`、`MATCH_READY`、`MATCH_FINISHED`、`SCRIPT_STOPPED`，异常包含 `ERROR`。事件文件采用 JSONL，包含 `schema_version`、`run_id`、递增 `seq` 和带时区时间戳。总控读取事件文件，普通 stdout 仍含调试日志。

开始由 `models/start.onnx` 识别；结束优先依据结算 OCR，也可能依据局内信号持续消失兜底。`result` 可为 `win/loss/draw/practice/unknown`，需同时查看 `result_source`、`source_event`。`unknown` 只说明检测到结束候选状态。

业务时间只计算 `MATCH_READY` 到 `MATCH_FINISHED`，登录、活动和匹配不计入局内时长。退出码：0 正常、1 业务错误、2 环境/参数或人工接管、3 外部停止、4 超时。

## 采集与指标

王者使用 `game_id=wzry`、`capture_app=honorofkings`、包名 `com.tencent.tmgp.sgame`。抓包、录屏、后处理复用采集工程；报告根据游戏标识显示标题。

本次合入了王者局内延迟文字 ROI，以及独立的实时卡顿阈值配置。ROI 适配既有布局，未在新分辨率上验收。为避免圆形 HUD 被误判为加载，王者实时加载圆环检测暂时关闭。卡顿判定准确性需要带标注的视频复核；游戏类型的离线指标仍受采集工程既有指标策略限制。

**本次未合入“直接复用实时 QoE 序列”的快速后处理路径。** 默认仍经过现有离线处理；真实 OCR 观测、插值/前向填充、缺失区间的统计口径尚需统一，不应把填充值当作独立实测样本。

`manifest.json` 的流程成功与采集/后处理质量分开判断。`report.md` 会显示缺失产物和后处理问题；文件存在或行数正常不构成数据质量验收。

## 多轮

`run_capture_probe.ps1 -Rounds 2` 使用共用多轮总控。首轮进入练习场，后续轮使用结算返回目标，失败后使用恢复目标。模拟测试已覆盖目标传递、重试和目录分配；连续两轮实机验证尚未完成。它不调用历史 `scheduler`，也不会自动切换到金铲铲。
