#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
生成样本元数据文件
根据采集结果生成yaml、qoe.csv、sa.csv等标注文件

使用方法:
    python generate_metadata.py --prefix mango_b29154b4_movie_360P_600_20260318T221434 --device b29154b4 --app mango --scene movie
"""

import csv
import yaml
import argparse
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from app_configs import get_app_config


def build_pcap_filename(prefix: str, pcap_ext: str = '.pcap') -> str:
    """Return the canonical capture filename without legacy prefixes."""
    extension = pcap_ext if pcap_ext.startswith('.') else f'.{pcap_ext}'
    return f"{prefix}{extension}"


def generate_yaml(
    prefix: str,
    device_id: str,
    device_info: Dict,
    app_name: str,
    app_package: str,
    app_type: str,
    scene: str,
    resolution: Optional[str],
    start_time: datetime,
    end_time: datetime,
    location: str = 'default',
    scene_params: Optional[Dict] = None,
    app_version: str = ''
) -> Dict:
    """生成YAML元数据"""
    
    yaml_data = {
        'version': '2.0.0',
        'time_zone': '+0800',
        'name': prefix,
        'task_info': {
            'start_time': start_time.strftime('%Y%m%dT%H%M%S%f')[:17] + '+0800',
            'end_time': end_time.strftime('%Y%m%dT%H%M%S%f')[:17] + '+0800',
            'action': [
                {
                    'time': start_time.strftime('%Y%m%dT%H%M%S%f')[:17] + '+0800',
                    'description': f'[start_application] start {app_package}.'
                },
                {
                    'time': end_time.strftime('%Y%m%dT%H%M%S%f')[:17] + '+0800',
                    'description': f'[stop_application] stop {app_package}.'
                }
            ]
        },
        'phone_info': {
            'location': location,
            'brand': device_info.get('brand', 'unknown'),
            'model': f"{device_info.get('brand', '')} {device_info.get('model', '')}".strip(),
            'os_version': device_info.get('os_version', 'unknown'),
        },
        'app_info': {
            'app_type': app_type,
            'app_name': app_name,
            'app_version': app_version,
            'app_package': app_package,
        },
        'qoe_info': {
            'qoe_start_time': start_time.strftime('%Y%m%dT%H%M%S%f')[:17] + '+0800'
        },
        'packet_capture': {
            'location': 'upf',
            'start_time': start_time.strftime('%Y%m%dT%H%M%S%f')[:17] + '+0800',
            'end_time': end_time.strftime('%Y%m%dT%H%M%S%f')[:17] + '+0800',
            'ipv4_changed': False,
            'ipv6_changed': False,
            'ipv4': device_info.get('ipv4', []),
            'ipv6': device_info.get('ipv6', [])
        },
        'video_record': {
            'start_time': start_time.strftime('%Y%m%dT%H%M%S%f')[:17] + '+0800',
            'end_time': end_time.strftime('%Y%m%dT%H%M%S%f')[:17] + '+0800',
            'height': device_info.get('screen_height', 0),
            'width': device_info.get('screen_width', 0),
            'mute': False,
            'fps': 60,
            'type': 'system_record'
        }
    }
    
    # 添加应用类型特定的配置
    app_specific = {
        'device': [f"android:///{device_id}"] if device_id else [],
        'pixel': resolution or 'auto',
        'scene': scene,
    }
    
    if scene_params:
        app_specific.update(scene_params)
    
    yaml_data['app_info'][app_type] = app_specific
    
    return yaml_data


def generate_qoe_csv(
    prefix: str,
    start_time: datetime,
    end_time: datetime,
    initial_resolution: int = -128,
    qoe_metrics: Optional[List[str]] = None,
    warmup_seconds: int = 0,
    pcap_ext: str = '.pcap'
) -> List[List]:
    """生成QoE CSV数据（占位/兜底数据）"""
    
    rows = []
    rows.append([
        'file_name', 'time', 'rtt', 'trust_resolution', 'trust_stall',
        'loading_reason'
    ])
    
    warmup_end = datetime.fromtimestamp(start_time.timestamp() + warmup_seconds)
    pcap_name = build_pcap_filename(prefix, pcap_ext)
    
    current = start_time
    while current <= end_time:
        if current < warmup_end:
            rtt, resolution, stall, business_started = -4, initial_resolution, -4, 0
        else:
            rtt, resolution, stall, business_started = -4, -4, 0, 1
        
        row = [
            pcap_name,
            current.strftime('%Y-%m-%d %H:%M:%S+0800'),
            rtt,
            resolution,
            stall,
            'placeholder',
        ]
        rows.append(row)
        current = datetime.fromtimestamp(current.timestamp() + 1)
    
    return rows


def generate_empty_qoe_csv(
    prefix: str,
    start_time: datetime,
    end_time: datetime,
    pcap_ext: str = '.pcap'
) -> List[List]:
    """
    生成QoE标注不适用文件。
    仅保留 file_name 和 time 两列，其余识别内容列为空。
    用于非视频/非游戏/非音视频通话类APP。
    """
    rows = []
    rows.append([
        'file_name', 'time', 'rtt', 'trust_resolution', 'trust_stall',
        'loading_reason'
    ])
    
    pcap_name = build_pcap_filename(prefix, pcap_ext)
    current = start_time
    while current <= end_time:
        row = [
            pcap_name,
            current.strftime('%Y-%m-%d %H:%M:%S+0800'),
            '', '', '', ''
        ]
        rows.append(row)
        current = datetime.fromtimestamp(current.timestamp() + 1)
    
    return rows


def generate_sa_csv_header() -> List[str]:
    """生成SA CSV表头"""
    return [
        'uri', 'app_type', 'app_name', 'packet_num', 'upper_packet_num',
        'down_packet_num', 'syn_flag', 'flow_bytes', 'upper_bytes', 'down_bytes',
        'srcip', 'dstip', 'srcport', 'dstport', 'l4proto', 'domain',
        'flow_start_time', 'not_in_app', 'egn_sub_protocol'
    ]


def save_yaml(data: Dict, output_path: str):
    """保存YAML文件"""
    with open(output_path, 'w', encoding='utf-8') as f:
        yaml.dump(data, f, allow_unicode=True, sort_keys=False)
    print(f"[生成] YAML文件: {output_path}")


def save_csv(rows: List[List], output_path: str):
    """保存CSV文件"""
    with open(output_path, 'w', newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        writer.writerows(rows)
    print(f"[生成] CSV文件: {output_path}")


def main():
    parser = argparse.ArgumentParser(
        description='生成样本元数据文件',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python generate_metadata.py --prefix mango_b29154b4_movie_360P_600_20260318T221434 \\
    --device b29154b4 --brand xiaomi --model "redmi k80" \\
    --app mango --package com.hunantv.imgo.activity \\
    --type video --scene movie --resolution "流畅360P" \\
    --start "2026-03-18 22:14:34" --end "2026-03-18 22:24:34"
        """
    )
    
    parser.add_argument('--prefix', '-p', required=True, help='文件名前缀')
    parser.add_argument('--device', '-d', required=True, help='设备ID')
    parser.add_argument('--brand', default='unknown', help='手机品牌')
    parser.add_argument('--model', default='unknown', help='手机型号')
    parser.add_argument('--os-version', default='unknown', help='系统版本')
    parser.add_argument('--app', '-a', required=True, help='应用名称')
    parser.add_argument('--package', required=True, help='应用包名')
    parser.add_argument('--type', '-t', required=True, help='应用类型(video/game/social)')
    parser.add_argument('--scene', '-s', required=True, help='采集场景')
    parser.add_argument('--resolution', '-r', help='视频分辨率')
    parser.add_argument('--start', required=True, help='开始时间(格式: YYYY-MM-DD HH:MM:SS)')
    parser.add_argument('--end', required=True, help='结束时间(格式: YYYY-MM-DD HH:MM:SS)')
    parser.add_argument('--location', '-l', default='default', help='采集地点')
    parser.add_argument('--output', '-o', default='./data', help='输出目录')
    
    args = parser.parse_args()
    
    # 解析时间
    try:
        start_time = datetime.strptime(args.start, '%Y-%m-%d %H:%M:%S')
        end_time = datetime.strptime(args.end, '%Y-%m-%d %H:%M:%S')
    except ValueError:
        print("错误: 时间格式错误，请使用 'YYYY-MM-DD HH:MM:SS'")
        return 1
    
    # 设备信息
    device_info = {
        'brand': args.brand,
        'model': args.model,
        'os_version': args.os_version,
        'ipv4': [],
        'ipv6': []
    }
    
    # 获取APP配置中的QoE指标
    app_config = get_app_config(args.app)
    qoe_metrics = app_config.qoe_metrics if app_config else []
    scene_params = {}
    if app_config:
        scene_config = app_config.get_scene(args.scene)
        if scene_config:
            scene_params = scene_config.params
    
    # 创建输出目录
    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)
    
    # 生成YAML
    yaml_data = generate_yaml(
        prefix=args.prefix,
        device_id=args.device,
        device_info=device_info,
        app_name=args.app,
        app_package=args.package,
        app_type=args.type,
        scene=args.scene,
        resolution=args.resolution,
        start_time=start_time,
        end_time=end_time,
        location=args.location,
        scene_params=scene_params
    )
    yaml_path = output_dir / f"{args.prefix}.yaml"
    save_yaml(yaml_data, str(yaml_path))
    
    # 生成QoE CSV
    qoe_rows = generate_qoe_csv(
        prefix=args.prefix,
        start_time=start_time,
        end_time=end_time,
        qoe_metrics=qoe_metrics
    )
    qoe_path = output_dir / f"{args.prefix}_qoe.csv"
    save_csv(qoe_rows, str(qoe_path))
    
    # 生成SA CSV（仅表头）
    sa_rows = [generate_sa_csv_header()]
    sa_path = output_dir / f"{args.prefix}_sa.csv"
    save_csv(sa_rows, str(sa_path))
    
    print(f"\n元数据文件生成完成!")
    print(f"输出目录: {output_dir}")
    
    return 0


if __name__ == '__main__':
    import sys
    sys.exit(main())
