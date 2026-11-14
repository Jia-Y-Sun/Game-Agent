from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from .processor import process_sample
from .sample import load_sample


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="录屏/截图离线 QoE 秒级标注（固定数据集 8 字段）")
    parser.add_argument("sample_dir", help="包含 YAML、MP4、PCAP 和截图的单个样本目录")
    parser.add_argument("--output", "-o", help="输出 CSV；默认覆盖样本目录中的 *_qoe.csv")
    parser.add_argument("--max-seconds", type=int, default=0, help="仅处理前 N 秒，0 表示完整样本")
    parser.add_argument("--skip-video-validation", action="store_true", help="跳过 FFmpeg 全量解码校验")
    parser.add_argument("--screenshots-only", action="store_true", help="忽略录屏，只使用时间戳截图作为证据")
    parser.add_argument("--ignore-source-stall", action="store_true", help="评估模式：卡顿只用录屏预测，不融合原QoE")
    parser.add_argument("--enable-vlm", action="store_true", help="启用视觉模型识别业务开始时间")
    parser.add_argument("--vlm-provider", choices=("qwen", "openai"), default="qwen")
    parser.add_argument("--vlm-model", default="qwen-vl-plus")
    parser.add_argument("--vlm-api-key", default="")
    parser.add_argument("--vlm-base-url", default="")
    parser.add_argument("--vlm-timeout-sec", type=int, default=30)
    parser.add_argument("--vlm-every", type=int, default=5, help="粗定位阶段每隔多少秒调用一次 VLM")
    parser.add_argument("--vlm-max-calls", type=int, default=20, help="单样本最大 VLM 调用次数")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.enable_vlm:
        os.environ["QOE_USE_VLM"] = "1"
        os.environ["QOE_VLM_PROVIDER"] = args.vlm_provider
        os.environ["QOE_VLM_MODEL"] = args.vlm_model
        os.environ["QOE_VLM_TIMEOUT_SEC"] = str(args.vlm_timeout_sec)
        if args.vlm_api_key:
            os.environ["QOE_VLM_API_KEY"] = args.vlm_api_key
        if args.vlm_base_url:
            os.environ["QOE_VLM_BASE_URL"] = args.vlm_base_url
    sample = load_sample(Path(args.sample_dir))
    output = Path(args.output).resolve() if args.output else sample.root / f"{sample.name}_qoe.csv"
    summary = process_sample(
        sample,
        output,
        max_seconds=args.max_seconds,
        validate=not args.skip_video_validation,
        vlm_every=args.vlm_every,
        screenshots_only=args.screenshots_only,
        use_source_stall=not args.ignore_source_stall,
        vlm_max_calls=args.vlm_max_calls,
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


