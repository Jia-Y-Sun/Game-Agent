# 源码与资源来源

整合日期：2026-11-09。

- 目标仓库基础版本：`arkenny79/jcc-auto-probe`，提交 `6b8f60f8348763bf01396b8110c1a49fb319ec1c`。金铲铲主体保留该版本，主要调整统一编排兼容与启动脚本。
- 王者源码：本机 `Game-Agent/wzry`，来源 checkout 提交 `78097fe`，其 remote 为 `https://github.com/Jia-Y-Sun/Game-Agent`。复制 tracked 源码及文档/小型素材后，在本仓库增加部署预检、ADB 选择、前置识别规则和测试。
- 王者原项目署名指向 `https://github.com/myBoris/wzry_ai`。迁入时的说明完整保留为 [README_UPSTREAM.md](../game-agent/wzry/README_UPSTREAM.md)，原作者图片和文档保留。
- 采集适配来自本机 `minipilot-main` 的游戏总控、报告和王者 QoE 调整，与目标仓库现有实现逐项合并；没有将整个本地采集目录覆盖上传。

本次不声称对上游代码、游戏素材或模型拥有新的许可授权。各上游原有声明保持适用；本次未统一设定新的开源许可证。模型再分发条件未核实，未提交模型二进制，也未提供未经确认的下载地址。

[模型清单](../game-agent/wzry/assets.example.json) 仅记录本机模型快照的相对文件名、大小及 SHA-256，供获准取得匹配文件后核对，不代表仓库包含这些资源。

旧研究文档保留实验历史；当前入口、已验证范围和未实现能力以 [根说明](../README.md) 和 [状态表](IMPLEMENTATION_STATUS.md) 为准。
