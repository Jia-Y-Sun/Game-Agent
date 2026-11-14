#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
智能语音采集调度器
支持自然语言命令，自动识别意图并调用 app_collector 和 qoe_monitor

使用方法:
    # 自然语言命令（采集 + QoE）
    python smart_collector.py "采集一个60秒的快手推荐流视频样本"
    
    # 自然语言命令（仅采集）
    python smart_collector.py "帮我采集王者荣耀对战5分钟"
    
    # 自然语言命令（仅QoE）
    python smart_collector.py "开始qoe监控"
    
    # 命令行参数方式
    python smart_collector.py --app-name kuaishou --scene feed --duration 60 --qoe
    
    # 交互模式
    python smart_collector.py
"""

import re
import sys
import time
import argparse
import threading
from pathlib import Path
from typing import Optional, Dict

from app_configs import get_app_config, ALL_APPS
from app_collector import AppCollector
from generate_metadata import build_pcap_filename
from qoe_monitor import QoEMonitor


class CommandParser:
    """自然语言命令解析器"""
    
    # APP别名映射（支持中文、英文、缩写）
    APP_ALIASES = {
        '快手': 'kuaishou', 'kuaishou': 'kuaishou', 'ks': 'kuaishou',
        '抖音': 'douyin', 'douyin': 'douyin', 'dy': 'douyin',
        '芒果': 'mango', '芒果tv': 'mango', 'mangotv': 'mango', 'mango': 'mango',
        '爱奇艺': 'iqiyi', 'iqiyi': 'iqiyi',
        '腾讯视频': 'tencent_video', '腾讯': 'tencent_video', 'tencent video': 'tencent_video',
        'b站': 'bilibili', '哔哩哔哩': 'bilibili', 'bilibili': 'bilibili',
        '小红书': 'xiaohongshu', 'xiaohongshu': 'xiaohongshu', 'xhs': 'xiaohongshu',
        '王者荣耀': 'honorofkings', '王者': 'honorofkings', 'honor of kings': 'honorofkings',
        '和平精英': 'pubgm', 'pubgm': 'pubgm',
        '原神': 'genshin', 'genshin': 'genshin',
        '微信': 'wechat', 'wechat': 'wechat',
        'qq': 'qq', '腾讯qq': 'qq',
        '淘宝': 'taobao', 'taobao': 'taobao',
        '京东': 'jd', 'jd': 'jd',
    }
    
    # 场景别名映射
    SCENE_ALIASES = {
        '推荐流': 'feed', 'feed': 'feed', '首页': 'feed', '发现页': 'feed',
        '关注流': 'following', 'following': 'following', '关注页': 'following',
        '直播': 'live', 'live': 'live', '直播间': 'live',
        '搜索': 'search', 'search': 'search',
        '电影': 'movie', 'movie': 'movie',
        '电视剧': 'tv_series', 'tv_series': 'tv_series', '剧集': 'tv_series',
        '综艺': 'variety', 'variety': 'variety',
        '短视频': 'short_video', 'short_video': 'short_video',
        '对战': 'battle', 'battle': 'battle', '5v5': 'battle',
        '大厅': 'lobby', 'lobby': 'lobby',
        '加载': 'loading', 'loading': 'loading',
        '游戏过程': 'gameplay', 'gameplay': 'gameplay',
        '文字聊天': 'text_chat', 'text_chat': 'text_chat',
        '语音通话': 'voice_call', 'voice_call': 'voice_call',
        '视频通话': 'video_call', 'video_call': 'video_call',
        '群视频通话': 'group_video_call', 'group_video': 'group_video_call',
        '朋友圈': 'moments', 'moments': 'moments',
        '浏览': 'browse', 'browse': 'browse', '商城浏览': 'browse',
        '故事': 'story', 'story': 'story', '快手故事': 'story',
        '视频流': 'video_feed', 'video_feed': 'video_feed',
    }
    
    @classmethod
    def parse(cls, command: str) -> Dict:
        result = {
            'raw': command,
            'mode': None,      # 'collect', 'qoe', 'collect_qoe'
            'app': None,
            'scene': None,
            'duration': None,
            'resolution': None,
            'device': None,
        }
        
        cmd_lower = command.lower()
        
        # 判断模式
        has_collect = any(k in command for k in ['采集', '收集', '录制', '录'])
        has_qoe = any(k in cmd_lower for k in ['qoe', '标注', '监控', '质量'])
        
        if has_collect and has_qoe:
            result['mode'] = 'collect_qoe'
        elif has_collect:
            result['mode'] = 'collect'
        elif has_qoe:
            result['mode'] = 'qoe'
        else:
            # 默认根据是否有APP推断
            result['mode'] = 'collect'
        
        # 提取时长：优先匹配秒，再匹配分钟
        sec_match = re.search(r'(\d+)\s*(?:秒|s|sec)', command, re.IGNORECASE)
        min_match = re.search(r'(\d+(?:\.\d+)?)\s*(?:分钟|分|min|m)(?:\b|[\s\u4e00-\u9fa5])', command, re.IGNORECASE)
        
        if sec_match:
            result['duration'] = int(sec_match.group(1))
        elif min_match:
            result['duration'] = int(float(min_match.group(1)) * 60)
        
        # 提取APP（优先匹配较长的别名，避免部分匹配）
        matched_apps = []
        for alias, app_name in cls.APP_ALIASES.items():
            if alias in command or alias in cmd_lower:
                matched_apps.append((len(alias), app_name, alias))
        
        if matched_apps:
            # 按别名长度降序，取最长的匹配
            matched_apps.sort(key=lambda x: x[0], reverse=True)
            result['app'] = matched_apps[0][1]
        
        # 提取场景（同样优先长别名）
        matched_scenes = []
        for alias, scene_name in cls.SCENE_ALIASES.items():
            if alias in command:
                matched_scenes.append((len(alias), scene_name, alias))
        
        if matched_scenes:
            matched_scenes.sort(key=lambda x: x[0], reverse=True)
            result['scene'] = matched_scenes[0][1]
        
        # 如果没有找到场景，使用APP默认场景
        if result['app'] and not result['scene']:
            app_config = get_app_config(result['app'])
            if app_config and app_config.scenes:
                result['scene'] = app_config.scenes[0].name
        
        # 提取分辨率（如 720P, 1080P）
        res_match = re.search(r'(360|480|720|1080|1440|2160)\s*[pP]?', command)
        if res_match:
            result['resolution'] = f"{res_match.group(1)}P"
        
        # 提取设备ID
        device_match = re.search(r'设备[:\s]+([a-zA-Z0-9]+)', command)
        if device_match:
            result['device'] = device_match.group(1)
        
        return result


class SmartExecutor:
    """智能执行器"""
    
    def __init__(self, parsed: Dict):
        self.parsed = parsed
        self.collector: Optional[AppCollector] = None
        self.monitor: Optional[QoEMonitor] = None
    
    def run(self) -> int:
        mode = self.parsed['mode']
        
        if mode == 'collect':
            return self._run_collect()
        elif mode == 'qoe':
            return self._run_qoe()
        elif mode == 'collect_qoe':
            return self._run_collect_and_qoe()
        else:
            print(f"[错误] 未知的执行模式: {mode}")
            return 1
    
    def _prepare_collector(self) -> bool:
        """校验参数并创建 AppCollector"""
        app = self.parsed['app']
        scene = self.parsed['scene']
        
        if not app or not scene:
            print("[错误] 需要指定APP和场景")
            self._print_parsed()
            return False
        
        app_config = get_app_config(app)
        if not app_config:
            print(f"[错误] 未找到应用: {app}")
            return False
        
        scene_config = app_config.get_scene(scene)
        if not scene_config:
            print(f"[错误] 应用 '{app}' 不存在场景 '{scene}'")
            print(f"[提示] 可用场景: {', '.join(s.name for s in app_config.scenes)}")
            return False
        
        duration = self.parsed.get('duration') or scene_config.duration
        
        self.collector = AppCollector(
            app_config=app_config,
            scene=scene,
            device_id=self.parsed.get('device'),
            duration=duration,
            resolution=self.parsed.get('resolution'),
            output_dir='./data',
        )
        return True
    
    def _run_collect(self) -> int:
        if not self._prepare_collector():
            return 1
        success = self.collector.run()
        return 0 if success else 1
    
    def _run_qoe(self) -> int:
        app = self.parsed.get('app')
        scene = self.parsed.get('scene')
        app_type = 'video'
        app_name = None
        if app:
            app_config = get_app_config(app)
            if app_config:
                app_type = app_config.app_type
                app_name = app_config.name
        else:
            app_type = self.parsed.get('app_type', 'video')
        
        self.monitor = QoEMonitor(
            device_id=self.parsed.get('device'),
            pcap_name=self.parsed.get('pcap_name'),
            app_type=app_type,
            app_name=app_name,
            scene=scene,
            register_signal=True,
        )
        return self.monitor.run()
    
    def _run_collect_and_qoe(self) -> int:
        if not self._prepare_collector():
            return 1
        
        # 构建pcap名称供QoE使用
        pcap_name = build_pcap_filename(self.collector.prefix)
        
        # 创建QoE监控器
        self.monitor = QoEMonitor(
            device_id=self.parsed.get('device'),
            pcap_name=pcap_name,
            app_type=app_config.app_type,
            app_name=app_config.name,
            scene=scene,
            interval=1.0,
            warmup_seconds=10,
            register_signal=False,
        )
        
        print("=" * 60)
        print("开始并发执行: 样本采集 + QoE标注")
        print(f"APP: {app} | 场景: {scene} | 时长: {duration}秒")
        print(f"pcap名称: {pcap_name}")
        print("=" * 60)
        
        # 启动采集线程
        collect_thread = threading.Thread(target=self._collect_worker, name="CollectorThread")
        collect_thread.start()
        
        # 等待采集开始（最多等60秒）
        print("[调度] 等待采集启动...")
        max_wait = 60
        waited = 0
        while not self.collector.start_time and waited < max_wait:
            time.sleep(0.5)
            waited += 0.5
            if not collect_thread.is_alive():
                break
        
        if not self.collector.start_time:
            print("[错误] 采集未正常启动，请检查设备连接")
            collect_thread.join()
            return 1
        
        print("[调度] 采集已启动，启动QoE监控...")
        
        # 启动QoE监控线程
        qoe_thread = threading.Thread(target=self._qoe_worker, name="QoeThread")
        qoe_thread.start()
        
        # 等待采集完成
        collect_thread.join()
        print("[调度] 采集已完成，正在停止QoE监控...")
        
        # 停止QoE
        self.monitor.running = False
        qoe_thread.join(timeout=15)
        
        if qoe_thread.is_alive():
            print("[警告] QoE监控线程未能正常结束")
        else:
            print("[调度] QoE监控已停止")
        
        print("\n" + "=" * 60)
        print("全部任务完成!")
        print(f"采集文件前缀: {self.collector.prefix}")
        if self.monitor and self.monitor.csv_path:
            print(f"QoE数据文件: {self.monitor.csv_path}")
        print("=" * 60)
        return 0
    
    def _collect_worker(self):
        try:
            self.collector.run()
        except Exception as e:
            print(f"\n[错误] 采集线程异常: {e}")
    
    def _qoe_worker(self):
        try:
            self.monitor.run()
        except Exception as e:
            print(f"\n[错误] QoE线程异常: {e}")
    
    def _print_parsed(self):
        print("\n解析结果:")
        for k, v in self.parsed.items():
            print(f"  {k}: {v}")


def build_parser_from_args() -> Dict:
    """从命令行参数构建解析结果"""
    parser = argparse.ArgumentParser(
        description='智能语音采集调度器',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python smart_collector.py "采集一个60秒的快手推荐流视频样本"
  python smart_collector.py "帮我采集王者荣耀对战5分钟并标注qoe"
  python smart_collector.py "开始qoe监控" --app-type game
  python smart_collector.py --app-name kuaishou --scene feed --duration 60 --qoe
        """
    )
    
    parser.add_argument('command', nargs='?', help='自然语言命令（如：采集一个60秒的快手推荐流视频样本）')
    parser.add_argument('--app-name', '-a', help='应用名称（如: kuaishou, honorofkings）')
    parser.add_argument('--scene', '-s', help='采集场景（如: feed, battle）')
    parser.add_argument('--duration', '-t', type=int, help='采集时长(秒)')
    parser.add_argument('--device', '-d', help='设备ID（adb devices查看）')
    parser.add_argument('--resolution', '-r', help='分辨率（如: 高清720P）')
    parser.add_argument('--qoe', action='store_true', help='同时执行QoE标注')
    parser.add_argument('--app-type', default='video', choices=['video', 'game', 'social', 'shopping', 'cloudgame'],
                       help='APP类型（仅QoE模式使用）')
    parser.add_argument('--pcap-name', '-p', help='pcap文件名（仅QoE模式使用）')
    parser.add_argument('--output', '-o', default='./data', help='输出目录')
    
    args = parser.parse_args()
    
    # 解析自然语言命令
    if args.command:
        parsed = CommandParser.parse(args.command)
    else:
        parsed = {'raw': '', 'mode': None, 'app': None, 'scene': None,
                  'duration': None, 'resolution': None, 'device': None}
    
    # 命令行参数覆盖解析结果
    if args.app_name:
        parsed['app'] = args.app_name
    if args.scene:
        parsed['scene'] = args.scene
    if args.duration is not None:
        parsed['duration'] = args.duration
    if args.device:
        parsed['device'] = args.device
    if args.resolution:
        parsed['resolution'] = args.resolution
    if args.qoe:
        parsed['mode'] = 'collect_qoe'
    if args.pcap_name:
        parsed['pcap_name'] = args.pcap_name
    parsed['app_type'] = args.app_type
    parsed['output_dir'] = args.output
    
    # 判断是否需要进入交互模式
    if not args.command and not args.app_name:
        parsed['interactive'] = True
    
    # 自动推断模式
    if not parsed.get('mode'):
        if args.pcap_name or (not parsed.get('app') and not args.app_name):
            parsed['mode'] = 'qoe'
        else:
            parsed['mode'] = 'collect'
    
    return parsed


def interactive_mode():
    """交互模式"""
    print("=" * 60)
    print("智能语音采集调度器")
    print("=" * 60)
    print("提示: 直接输入自然语言命令，例如:")
    print('  "采集一个60秒的快手推荐流视频样本"')
    print('  "帮我采集王者荣耀对战5分钟并标注qoe"')
    print('  "开始qoe监控"')
    print("输入 'exit' 或 'quit' 退出")
    print("=" * 60)
    
    while True:
        try:
            command = input("\n>>> ").strip()
            if command.lower() in ('exit', 'quit', 'q'):
                break
            if not command:
                continue
            
            parsed = CommandParser.parse(command)
            print("\n[解析结果]")
            for k, v in parsed.items():
                if v is not None:
                    print(f"  {k}: {v}")
            
            # 参数校验提示
            if not parsed['app'] and parsed['mode'] != 'qoe':
                print("\n[警告] 未能识别APP名称，请检查输入或直接使用 --app-name 参数")
                continue
            
            if not parsed['scene'] and parsed['mode'] != 'qoe':
                print("\n[警告] 未能识别场景，将使用APP默认场景")
            
            # 确认执行
            confirm = input("\n确认执行? [Y/n] ").strip().lower()
            if confirm and confirm not in ('y', 'yes'):
                print("已取消")
                continue
            
            executor = SmartExecutor(parsed)
            executor.run()
            
        except KeyboardInterrupt:
            print("\n已退出")
            break
        except EOFError:
            break


def main():
    parsed = build_parser_from_args()
    
    if parsed.get('interactive'):
        interactive_mode()
        return 0
    
    # 打印解析结果（方便调试）
    if parsed.get('raw'):
        print("[解析结果]")
        for k, v in parsed.items():
            if v is not None and k not in ('interactive',):
                print(f"  {k}: {v}")
        print()
    
    executor = SmartExecutor(parsed)
    return executor.run()


if __name__ == '__main__':
    sys.exit(main())
