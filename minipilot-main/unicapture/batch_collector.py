#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
批量采集脚本
支持通过配置文件批量执行多个APP、多个场景的采集任务

使用方法:
    python batch_collector.py --config batch_config.yaml
"""

import os
import sys
import time
import yaml
import argparse
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Optional

from app_configs import get_app_config, AppConfig
from app_collector import AppCollector


class BatchCollector:
    """批量采集器"""
    
    def __init__(self, config_path: str, capture_mode: str = 'auto', pcap_app_package: Optional[str] = None, enable_qoe: bool = True):
        self.config_path = config_path
        self.config = self._load_config()
        self.results: List[Dict] = []
        self.capture_mode = capture_mode
        self.pcap_app_package = pcap_app_package
        self.enable_qoe = enable_qoe
    
    def _load_config(self) -> Dict:
        """加载配置文件"""
        with open(self.config_path, 'r', encoding='utf-8') as f:
            return yaml.safe_load(f)
    
    def _get_devices(self) -> List[Dict]:
        """获取设备列表"""
        return self.config.get('devices', [])
    
    def _get_apps(self) -> List[Dict]:
        """获取APP列表"""
        return self.config.get('apps', [])
    
    def _get_schedule(self) -> Dict:
        """获取调度配置"""
        return self.config.get('schedule', {'repeat': 1, 'interval': 60})
    
    def _build_tasks(self) -> List[Dict]:
        """构建任务列表"""
        tasks = []
        devices = self._get_devices()
        apps = self._get_apps()
        schedule = self._get_schedule()
        repeat = schedule.get('repeat', 1)
        
        for device in devices:
            for app_config in apps:
                app_name = app_config.get('name')
                app = get_app_config(app_name)
                
                if not app:
                    print(f"[警告] 未找到应用: {app_name}")
                    continue
                
                scenes = app_config.get('scenes', [])
                for scene_config in scenes:
                    scene_name = scene_config.get('name')
                    
                    # 检查场景是否存在
                    if not app.get_scene(scene_name):
                        print(f"[警告] 场景 '{scene_name}' 不存在于应用 '{app_name}'")
                        continue
                    
                    # 获取分辨率列表
                    resolutions = scene_config.get('resolutions', [None])
                    duration = scene_config.get('duration')
                    
                    for resolution in resolutions:
                        for i in range(repeat):
                            tasks.append({
                                'device': device,
                                'app': app,
                                'scene_name': scene_name,
                                'scene_config': scene_config,
                                'resolution': resolution,
                                'duration': duration,
                                'repeat_index': i + 1,
                                'total_repeat': repeat
                            })
        
        return tasks
    
    def run(self):
        """执行批量采集"""
        tasks = self._build_tasks()
        schedule = self._get_schedule()
        interval = schedule.get('interval', 60)
        
        print("="*60)
        print("批量采集任务")
        print("="*60)
        print(f"总任务数: {len(tasks)}")
        print(f"任务间隔: {interval}秒")
        print("="*60)
        
        for i, task in enumerate(tasks, 1):
            device = task['device']
            app = task['app']
            scene_name = task['scene_name']
            resolution = task['resolution']
            duration = task['duration']
            repeat_idx = task['repeat_index']
            total_repeat = task['total_repeat']
            
            print(f"\n{'='*60}")
            print(f"任务 {i}/{len(tasks)}")
            print(f"应用: {app.name} ({app.app_type})")
            print(f"场景: {scene_name}")
            print(f"分辨率: {resolution or '自动'}")
            print(f"设备: {device.get('brand', 'unknown')} {device.get('model', 'unknown')} ({device.get('id', 'unknown')})")
            print(f"轮次: {repeat_idx}/{total_repeat}")
            print(f"{'='*60}")
            
            try:
                collector = AppCollector(
                    app_config=app,
                    scene=scene_name,
                    device_id=device.get('id'),
                    duration=duration,
                    resolution=resolution,
                    output_dir=self.config.get('output_dir', './data'),
                    location=device.get('location', 'default'),
                    pcap_mode=self.config.get('pcap_mode', 'standard'),
                    capture_mode=self.capture_mode,
                    pcap_app_package=self.pcap_app_package or self.config.get('pcap_app_package'),
                    enable_qoe=self.enable_qoe
                )
                
                success = collector.run()
                
                self.results.append({
                    'task_index': i,
                    'app': app.name,
                    'scene': scene_name,
                    'resolution': resolution,
                    'device': device.get('id'),
                    'success': success,
                    'timestamp': datetime.now().isoformat()
                })
                
            except Exception as e:
                print(f"[错误] 任务失败: {e}")
                self.results.append({
                    'task_index': i,
                    'app': app.name,
                    'scene': scene_name,
                    'resolution': resolution,
                    'device': device.get('id'),
                    'success': False,
                    'error': str(e),
                    'timestamp': datetime.now().isoformat()
                })
            
            # 任务间隔
            if i < len(tasks):
                print(f"\n[等待] 等待 {interval} 秒后继续下一个任务...")
                time.sleep(interval)
        
        # 打印汇总报告
        self._print_summary()
    
    def _print_summary(self):
        """打印汇总报告"""
        total = len(self.results)
        success = sum(1 for r in self.results if r.get('success'))
        failed = total - success
        
        print("\n" + "="*60)
        print("批量采集完成")
        print("="*60)
        print(f"总任务数: {total}")
        print(f"成功: {success}")
        print(f"失败: {failed}")
        print("="*60)
        
        if failed > 0:
            print("\n失败任务:")
            for r in self.results:
                if not r.get('success'):
                    print(f"  - 任务{r['task_index']}: {r['app']}/{r['scene']} - {r.get('error', '未知错误')}")


def create_sample_config():
    """创建示例配置文件"""
    sample_config = {
        'devices': [
            {
                'id': 'b29154b4',
                'brand': 'xiaomi',
                'model': 'redmi k80',
                'location': 'lab_shanghai'
            }
        ],
        'apps': [
            {
                'name': 'mango',
                'scenes': [
                    {
                        'name': 'movie',
                        'resolutions': ['流畅360P', '高清720P', '超清1080P'],
                        'duration': 600
                    },
                    {
                        'name': 'tv_series',
                        'resolutions': ['流畅360P', '高清720P'],
                        'duration': 600
                    }
                ]
            },
            {
                'name': 'honorofkings',
                'scenes': [
                    {
                        'name': 'battle',
                        'duration': 900
                    },
                    {
                        'name': 'lobby',
                        'duration': 300
                    }
                ]
            },
            {
                'name': 'wechat',
                'scenes': [
                    {
                        'name': 'video_call',
                        'duration': 300
                    },
                    {
                        'name': 'voice_call',
                        'duration': 300
                    }
                ]
            }
        ],
        'schedule': {
            'repeat': 3,  # 每个场景重复3次
            'interval': 60  # 任务间隔60秒
        },
        'output_dir': './batch_data'
    }
    
    config_path = 'batch_config_sample.yaml'
    with open(config_path, 'w', encoding='utf-8') as f:
        yaml.dump(sample_config, f, allow_unicode=True, sort_keys=False)
    
    print(f"已创建示例配置文件: {config_path}")
    return config_path


def main():
    parser = argparse.ArgumentParser(
        description='批量采集工具',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  # 使用配置文件执行批量采集
  python batch_collector.py --config batch_config.yaml
  
  # 生成示例配置文件
  python batch_collector.py --create-sample
        """
    )
    
    parser.add_argument('--config', '-c', help='配置文件路径')
    parser.add_argument('--create-sample', action='store_true', help='创建示例配置文件')
    parser.add_argument('--capture-mode', choices=['auto', 'root', 'noroot'], default='auto',
                       help='采集模式: auto(自动), root(tcpdump需root), noroot(VpnService App无root)')
    parser.add_argument('--pcap-app-package', help='指定无root抓包App的包名')
    parser.add_argument('--enable-qoe', action='store_true', default=True, help='启用QoE实时监测(默认开启)')
    parser.add_argument('--disable-qoe', action='store_true', help='禁用QoE实时监测')
    
    args = parser.parse_args()
    
    # 创建示例配置
    if args.create_sample:
        path = create_sample_config()
        print(f"\n请修改 {path} 后运行:")
        print(f"  python batch_collector.py --config {path}")
        return 0
    
    # 检查配置文件
    if not args.config:
        print("错误: 请指定配置文件或使用 --create-sample 创建示例配置")
        parser.print_help()
        return 1
    
    if not os.path.exists(args.config):
        print(f"错误: 配置文件不存在: {args.config}")
        return 1
    
    # 运行批量采集
    collector = BatchCollector(args.config, capture_mode=args.capture_mode, pcap_app_package=args.pcap_app_package, enable_qoe=args.enable_qoe and not args.disable_qoe)
    collector.run()
    
    return 0


if __name__ == '__main__':
    sys.exit(main())
