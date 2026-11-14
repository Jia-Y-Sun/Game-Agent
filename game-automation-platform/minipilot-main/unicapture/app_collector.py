#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
安卓端侧APP样本采集主控脚本
支持视频、游戏、社交、购物等不同领域APP的自动化采集

核心特性:
  - 支持 Root 模式(tcpdump) 和 无Root 模式(PCAPdroid等VpnService App)
  - 自动检测设备是否root，智能选择抓包方式(--capture-mode auto)
  - 输出六种标准格式: pcap, yaml, qoe.csv, sa.csv, mp4, log

使用方法:
    # 自动模式(优先root, 无root则使用PCAPdroid)
    python app_collector.py --app-name mango --scene movie --duration 600
    
    # 强制无root模式(依赖已安装的PCAPdroid)
    python app_collector.py --app-name mango --scene movie --capture-mode noroot
    
    # 强制root模式
    python app_collector.py --app-name mango --scene movie --capture-mode root
    
    # 使用自定义抓包App
    python app_collector.py --app-name mango --scene movie --capture-mode noroot --pcap-app-package com.xxx.xxx
"""

import os
import sys
import csv
import time
import argparse
import subprocess
import threading
import signal
import shutil
import yaml
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, Dict, List

from app_configs import get_app_config, AppConfig, SceneConfig, is_qoe_applicable
from generate_metadata import (
    build_pcap_filename,
    generate_yaml,
    generate_qoe_csv,
    generate_empty_qoe_csv,
    generate_sa_csv_header,
)
try:
    from .video_metadata import probe_video_duration
except ImportError:  # Direct ``python app_collector.py`` execution.
    from video_metadata import probe_video_duration

# 尝试导入scapy用于pcap解析；首次使用若未安装则自动安装
def _ensure_scapy():
    """检测并自动安装 scapy"""
    try:
        from scapy.all import rdpcap, IP, TCP, UDP
        return True, rdpcap, IP, TCP, UDP
    except ImportError:
        print("[提示] 首次使用，正在自动安装 scapy...")
        try:
            subprocess.check_call(
                [sys.executable, "-m", "pip", "install", "scapy>=2.5.0"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            from scapy.all import rdpcap, IP, TCP, UDP
            print("[成功] scapy 安装完成")
            return True, rdpcap, IP, TCP, UDP
        except Exception as e:
            print(f"[警告] scapy 自动安装失败: {e}")
            print("[提示] 可手动运行: pip install scapy>=2.5.0")
            return False, None, None, None, None


_scapy_result = _ensure_scapy()
SCAPY_AVAILABLE = _scapy_result[0]
if SCAPY_AVAILABLE:
    rdpcap, IP, TCP, UDP = _scapy_result[1], _scapy_result[2], _scapy_result[3], _scapy_result[4]
else:
    rdpcap = IP = TCP = UDP = None

# 尝试导入QoE监测模块
try:
    import qoe_monitor as qm_module
    QOE_AVAILABLE = True
except ImportError:
    QOE_AVAILABLE = False


class ADBHelper:
    """ADB工具类，支持Root和无Root两种模式"""

    PCAPDROID_CAPTURE_ACTIVITY = (
        'com.emanuelef.remote_capture.activities.CaptureCtrl'
    )
    
    # 常见无root抓包App包名映射
    PCAP_APPS = {
        'pcapdroid': [
            'com.emanuelef.remote_capture.debug',  # PCAPdroid debug build
            'com.emanuelef.remote_capture',  # PCAPdroid 实际包名
            'com.emanuele.falconi.pcapdroid',
            'com.emanuele.pcapdroid',
        ],
        'httpcanary': [
            'com.guoshi.httpcanary',
            'com.guoshi.httpcanary.premium',
        ],
        'netcapture': [
            'com.laruin.netcapture',
        ],
    }
    
    def __init__(self, device_id: Optional[str] = None):
        self.device_id = device_id
        self.base_cmd = self._build_base_cmd(device_id)
        self.connection_error = ''
        
        self._root_available: Optional[bool] = None
        self._detected_pcap_app: Optional[str] = None
        self._detected_pcap_package: Optional[str] = None
        self._logcat_file_handle = None

    @staticmethod
    def _build_base_cmd(device_id: Optional[str]) -> list:
        adb_binary = (
            os.getenv('MINI_PILOT_ADB_BIN')
            or os.getenv('ADB_BIN')
            or 'adb'
        )
        return [adb_binary, '-s', device_id] if device_id else [adb_binary]

    def _select_device(self, device_id: str) -> None:
        """Pin every subsequent device operation to one ADB serial."""
        self.device_id = device_id
        self.base_cmd = self._build_base_cmd(device_id)
    
    @property
    def root_available(self) -> bool:
        """懒检测root权限"""
        if self._root_available is None:
            self._root_available = self._check_root()
        return self._root_available
    
    def _check_root(self) -> bool:
        """检测设备是否有root权限"""
        success, stdout, _ = self.run(['shell', 'su', '-c', 'id'], timeout=5)
        if success and 'uid=0' in stdout:
            return True
        # 备用检测
        success, _, _ = self.run(['shell', 'su', '-c', 'echo rooted'], timeout=5)
        return success
    
    def run(self, cmd: list, capture_output: bool = True, timeout: int = 30) -> tuple:
        """运行ADB命令"""
        full_cmd = self.base_cmd + cmd
        try:
            result = subprocess.run(
                full_cmd,
                capture_output=capture_output,
                text=True,
                encoding='utf-8',
                errors='replace',
                timeout=timeout
            )
            return result.returncode == 0, result.stdout, result.stderr
        except subprocess.TimeoutExpired:
            return False, "", "Command timeout"
        except Exception as e:
            return False, "", str(e)
    
    def run_shell(self, cmd: str, use_root: Optional[bool] = None, timeout: int = 30) -> tuple:
        """
        执行shell命令，智能决定是否使用su
        
        Args:
            cmd: shell命令字符串
            use_root: 是否强制使用root，None则自动判断
            timeout: 超时时间
        """
        if use_root is None:
            use_root = self.root_available
        
        if use_root:
            # 转义cmd中的双引号
            escaped = cmd.replace('"', '\\"')
            full_cmd = f'su -c "{escaped}"'
        else:
            full_cmd = cmd
        
        return self.run(['shell', full_cmd], capture_output=True, timeout=timeout)
    
    def check_connection(self) -> bool:
        """Resolve one ready device and pin all later commands to its serial."""
        self.connection_error = ''
        try:
            adb_binary = (
                os.getenv('MINI_PILOT_ADB_BIN')
                or os.getenv('ADB_BIN')
                or 'adb'
            )
            result = subprocess.run(
                [adb_binary, 'devices'],
                capture_output=True,
                text=True,
                encoding='utf-8',
                errors='replace',
                timeout=10,
            )
        except Exception as exc:
            self.connection_error = f"无法执行 adb devices: {exc}"
            return False
        if result.returncode != 0:
            self.connection_error = (
                result.stderr.strip() or result.stdout.strip() or 'adb devices 执行失败'
            )
            return False

        ready_devices = []
        for line in result.stdout.splitlines()[1:]:
            parts = line.split()
            if len(parts) >= 2 and parts[1] == 'device':
                ready_devices.append(parts[0])

        if self.device_id:
            if self.device_id not in ready_devices:
                self.connection_error = f"设备未连接或未就绪: {self.device_id}"
                return False
            self._select_device(self.device_id)
            return True

        if len(ready_devices) == 1:
            self._select_device(ready_devices[0])
            print(f"[设备] 自动选择唯一设备: {self.device_id}")
            return True
        if not ready_devices:
            self.connection_error = '未检测到已就绪的 Android 设备'
        else:
            self.connection_error = (
                '检测到多个 Android 设备，请使用 --device 指定序列号: '
                + ', '.join(ready_devices)
            )
        return False
    
    def get_device_info(self) -> Dict:
        """获取设备信息"""
        info = {}
        
        # 品牌
        success, stdout, _ = self.run(['shell', 'getprop', 'ro.product.brand'])
        if success:
            info['brand'] = stdout.strip()
        
        # 型号
        success, stdout, _ = self.run(['shell', 'getprop', 'ro.product.model'])
        if success:
            info['model'] = stdout.strip()
        
        # OS版本
        success, stdout, _ = self.run(['shell', 'getprop', 'ro.build.version.release'])
        if success:
            info['os_version'] = stdout.strip()
        
        # Android API等级
        success, stdout, _ = self.run(['shell', 'getprop', 'ro.build.version.sdk'])
        if success:
            info['api_level'] = stdout.strip()
        
        # 屏幕分辨率
        success, stdout, _ = self.run(['shell', 'wm', 'size'])
        if success:
            for line in stdout.split('\n'):
                if 'Physical size' in line:
                    info['resolution'] = line.split(':')[-1].strip()
                    try:
                        w, h = info['resolution'].split('x')
                        info['screen_width'] = int(w.strip())
                        info['screen_height'] = int(h.strip())
                    except:
                        pass
        
        # 获取IP地址
        info['ipv4'] = []
        info['ipv6'] = []
        
        success, stdout, _ = self.run(['shell', 'ip', 'addr', 'show', 'wlan0'], timeout=10)
        if not success:
            success, stdout, _ = self.run(['shell', 'ip', 'addr'], timeout=10)
        
        if success and stdout:
            for line in stdout.split('\n'):
                line = line.strip()
                if 'inet ' in line and '127.0.0.1' not in line:
                    parts = line.split()
                    for i, part in enumerate(parts):
                        if part == 'inet' and i + 1 < len(parts):
                            ip = parts[i + 1].split('/')[0]
                            if ip not in info['ipv4'] and not ip.startswith('127.'):
                                info['ipv4'].append(ip)
                elif 'inet6 ' in line:
                    parts = line.split()
                    for i, part in enumerate(parts):
                        if part == 'inet6' and i + 1 < len(parts):
                            ip = parts[i + 1].split('/')[0]
                            if ip not in info['ipv6'] and not ip.startswith('fe80'):
                                info['ipv6'].append(ip)
        
        # 备用方案：使用ifconfig
        if not info['ipv4']:
            success, stdout, _ = self.run(['shell', 'ifconfig'], timeout=10)
            if success and stdout:
                for line in stdout.split('\n'):
                    if 'inet addr:' in line:
                        import re
                        match = re.search(r'inet addr:(\d+\.\d+\.\d+\.\d+)', line)
                        if match:
                            ip = match.group(1)
                            if ip not in info['ipv4'] and not ip.startswith('127.'):
                                info['ipv4'].append(ip)
        
        return info
    
    def clear_app(self, package: str) -> bool:
        """清理应用数据"""
        success, _, _ = self.run(['shell', 'pm', 'clear', package])
        return success
    
    def start_app(self, package: str, activity: Optional[str] = None) -> bool:
        """启动应用"""
        if activity:
            success, _, _ = self.run(['shell', 'am', 'start', '-n', f'{package}/{activity}'])
        else:
            success, _, _ = self.run(['shell', 'monkey', '-p', package, '-c', 'android.intent.category.LAUNCHER', '1'])
        return success
    
    def stop_app(self, package: str) -> bool:
        """停止应用"""
        success, _, _ = self.run(['shell', 'am', 'force-stop', package])
        return success
    
    def detect_pcap_app(self, preferred: Optional[str] = None) -> Optional[str]:
        """
        检测设备上安装的无root抓包App
        
        Returns:
            检测到的包名，或None
        """
        if self._detected_pcap_package:
            return self._detected_pcap_package
        
        success, stdout, _ = self.run(['shell', 'pm', 'list', 'packages'], timeout=15)
        if not success:
            return None
        
        installed = [line.replace('package:', '').strip() 
                     for line in stdout.strip().split('\n') 
                     if line.startswith('package:')]
        
        # 如果指定了优先应用
        if preferred and preferred in self.PCAP_APPS:
            for pkg in self.PCAP_APPS[preferred]:
                if pkg in installed:
                    self._detected_pcap_app = preferred
                    self._detected_pcap_package = pkg
                    return pkg
        
        # 自动检测所有已知应用
        for app_name, packages in self.PCAP_APPS.items():
            for pkg in packages:
                if pkg in installed:
                    self._detected_pcap_app = app_name
                    self._detected_pcap_package = pkg
                    return pkg
        
        return None
    
    def get_detected_pcap_info(self) -> tuple:
        """返回检测到的抓包App信息 (app_name, package_name)"""
        return self._detected_pcap_app, self._detected_pcap_package
    
    def start_pcapdroid_capture(self, output_name: str = "capture.pcap",
                                 app_package: str = None, api_key: str = None,
                                 pcapdroid_package: str = None) -> bool:
        """
        通过PCAPdroid Intent API自动启动抓包
        
        Args:
            output_name: 保存的pcap文件名
            app_package: 目标应用包名（过滤只抓该应用）
            api_key: PCAPdroid Control Permissions中生成的API Key（跳过授权对话框）
            pcapdroid_package: 实际安装的PCAPdroid包名
        """
        pkg = (pcapdroid_package or self._detected_pcap_package
               or 'com.emanuelef.remote_capture')
        activity = f'{pkg}/{self.PCAPDROID_CAPTURE_ACTIVITY}'
        
        print(f"[抓包] 通过Intent API启动PCAPdroid ({pkg})...")
        print(f"[抓包] 文件: {output_name}")
        if app_package:
            print(f"[抓包] 过滤应用: {app_package}")
        
        # 构建Intent命令
        cmd = [
            'shell', 'am', 'start',
            '-e', 'action', 'start',
            '-e', 'pcap_dump_mode', 'pcap_file',
            '-e', 'pcap_name', output_name,
            '-n', activity
        ]
        
        if app_package:
            cmd.extend(['-e', 'app_filter', app_package])
        if api_key:
            cmd.extend(['-e', 'api_key', api_key])
        
        success, stdout, stderr = self.run(cmd, timeout=10)
        if not success:
            print(f"[警告] 发送Intent失败: {stderr}")
            return False
        
        # 等待VPN服务启动
        print("[抓包] 等待PCAPdroid启动VPN...")
        time.sleep(2)
        
        # 检查是否运行（通过检查进程）
        for i in range(5):
            ps_success, ps_stdout, _ = self.run(['shell', 'ps', '-A'], timeout=5)
            if ps_success and pkg in ps_stdout:
                print("[抓包] PCAPdroid抓包已启动")
                return True
            time.sleep(1)
        
        print("[警告] PCAPdroid可能未成功启动")
        print("[提示] 首次使用需在手机上授权，建议打开PCAPdroid → 设置 → Control Permissions → 允许此设备控制")
        print("[提示] 或生成API Key后通过 --pcapdroid-api-key 参数传入")
        return False
    
    def stop_pcapdroid_capture(self, api_key: str = None,
                                pcapdroid_package: str = None) -> bool:
        """通过PCAPdroid Intent API自动停止抓包"""
        pkg = (pcapdroid_package or self._detected_pcap_package
               or 'com.emanuelef.remote_capture')
        activity = f'{pkg}/{self.PCAPDROID_CAPTURE_ACTIVITY}'
        
        print(f"[抓包] 通过Intent API停止PCAPdroid ({pkg})...")
        cmd = ['shell', 'am', 'start', '-e', 'action', 'stop', '-n', activity]
        if api_key:
            cmd.extend(['-e', 'api_key', api_key])
        self.run(cmd, timeout=10)
        
        # 等待文件保存完成
        print("[抓包] 等待文件保存...")
        time.sleep(3)
        
        # 确认已停止
        for i in range(5):
            ps_success, ps_stdout, _ = self.run(['shell', 'ps', '-A'], timeout=5)
            if not ps_success or pkg not in ps_stdout:
                print("[抓包] PCAPdroid已停止")
                return True
            time.sleep(1)
        
        return True
    
    def start_pcap_app(self, package: str, output_hint: str = "") -> bool:
        """
        启动无root抓包App（通用fallback方式）
        
        对于不支持Intent API的抓包App，启动App并提示用户手动操作。
        """
        print(f"[抓包] 启动无root抓包App: {package}")
        
        # 尝试启动主Activity
        success, _, _ = self.run(['shell', 'monkey', '-p', package, '-c', 'android.intent.category.LAUNCHER', '1'])
        if not success:
            success, _, _ = self.run(['shell', 'am', 'start', '-a', 'android.intent.action.MAIN', 
                                      '-c', 'android.intent.category.LAUNCHER', '-n', package])
        
        if not success:
            print(f"[错误] 无法启动 {package}，请手动打开")
            return False
        
        time.sleep(2)
        
        print("\n" + "="*60)
        print("[抓包] 无Root抓包模式 - 请完成以下手机端操作")
        print("="*60)
        print("1. 在手机上允许VPN权限请求（首次使用）")
        print("2. 点击'开始捕获'或类似按钮启动抓包")
        print("3. 建议设置过滤: 仅捕获目标应用流量")
        print("4. 建议设置导出格式: PCAP (标准格式)")
        if output_hint:
            print(f"5. 建议保存路径: {output_hint}")
        print("="*60)
        
        try:
            input("[抓包] 请在手机上开始抓包，完成后按回车键继续...")
        except EOFError:
            print("[抓包] 非交互模式，等待8秒...")
            time.sleep(8)
        
        return True
    
    def stop_pcap_app(self, package: str) -> bool:
        """停止无root抓包App（通用fallback方式）"""
        print("\n[抓包] 请在手机上停止抓包并保存文件")
        try:
            input("[抓包] 完成后按回车键继续...")
        except EOFError:
            print("[抓包] 非交互模式，等待5秒...")
            time.sleep(5)
        return True
    
    def find_pcap_files(self, search_paths: Optional[List[str]] = None) -> List[str]:
        """
        在设备上搜索pcap/pcapng文件
        
        Args:
            search_paths: 指定搜索路径列表，默认搜索常见路径
        """
        if search_paths is None:
            pcapdroid_pkg = (self._detected_pcap_package
                             or 'com.emanuelef.remote_capture')
            search_paths = [
                '/sdcard/Download/PCAPdroid/',
                '/sdcard/Download/',
                '/sdcard/PCAPdroid/',
                '/sdcard/Documents/PCAPdroid/',
                f'/sdcard/Android/data/{pcapdroid_pkg}/files/',
                '/sdcard/Download/HttpCanary/',
            ]
        
        found_files = []
        
        # 方法1: 使用find命令（最可靠）
        find_cmd = r"find /sdcard -maxdepth 4 \( -name '*.pcap' -o -name '*.pcapng' \) 2>/dev/null"
        success, stdout, _ = self.run(['shell', find_cmd], timeout=15)
        if success and stdout:
            for line in stdout.strip().split('\n'):
                line = line.strip()
                if line and (line.endswith('.pcap') or line.endswith('.pcapng')):
                    if line not in found_files:
                        found_files.append(line)
        
        # 方法2: 如果find失败，逐个目录检查
        if not found_files:
            for path in search_paths:
                for ext in ['pcap', 'pcapng']:
                    cmd = f'for f in {path}*.{ext}; do [ -f \"$f\" ] && echo \"$f\"; done'
                    success, stdout, _ = self.run(['shell', cmd], timeout=10)
                    if success and stdout:
                        for line in stdout.strip().split('\n'):
                            line = line.strip()
                            if line and line.endswith(f'.{ext}') and line not in found_files:
                                found_files.append(line)
        
        # 按修改时间排序（最新的在前）
        if found_files:
            def get_mtime(path):
                success, stdout, _ = self.run(['shell', f'stat -c %Y "{path}" 2>/dev/null'], timeout=5)
                if success:
                    try:
                        return int(stdout.strip())
                    except:
                        pass
                return 0
            found_files.sort(key=get_mtime, reverse=True)
        
        return found_files
    
    def start_screenrecord(self, save_path: str = '/sdcard/Download/pcap_collector/screenrecord.mp4', 
                          time_limit: int = 600,
                          use_root: Optional[bool] = None) -> subprocess.Popen:
        """
        开始录屏
        
        Args:
            save_path: 保存路径（在设备上）
            time_limit: 录屏时长限制(秒)，默认600秒，最大1800秒
            use_root: 是否使用root执行，None则自动判断
        """
        if use_root is None:
            use_root = self.root_available
        
        time_limit = min(time_limit + 30, 180)
        
        # 确保目录存在
        save_dir = os.path.dirname(save_path)
        if use_root:
            self.run_shell(f'mkdir -p {save_dir}', use_root=True)
        else:
            self.run(['shell', 'mkdir', '-p', save_dir])
        
        if use_root:
            cmd = self.base_cmd + ['shell', f'su -c "screenrecord --time-limit {time_limit} {save_path}"']
        else:
            cmd = self.base_cmd + ['shell', 'screenrecord', '--time-limit', str(time_limit), save_path]
        
        print(f"[录屏] 启动录屏: {save_path}")
        if not use_root:
            print("[录屏] 使用无root模式")
        
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding='utf-8',
            errors='replace'
        )
        
        time.sleep(1.5)
        if process.poll() is not None:
            stderr = process.stderr.read() if process.stderr else ""
            print(f"[警告] screenrecord启动失败: {stderr}")
            if not use_root and ('permission' in stderr.lower() or 'denied' in stderr.lower()):
                print("[提示] 设备可能需要root权限才能录屏，尝试root模式...")
                return self.start_screenrecord(save_path, time_limit, use_root=True)
            return None
        
        time.sleep(0.5)
        check_cmd = self.base_cmd + ['shell', 'ls', '-la', save_path]
        result = subprocess.run(check_cmd, capture_output=True, text=True, timeout=5)
        if result.returncode == 0:
            print(f"[录屏] 录屏已开始: {result.stdout.strip()}")
        
        return process
    
    def stop_screenrecord(self, process: subprocess.Popen, 
                         save_path: str = '/sdcard/Download/pcap_collector/screenrecord.mp4',
                         use_root: Optional[bool] = None) -> bool:
        """停止录屏 - 确保文件完整写入"""
        if use_root is None:
            use_root = self.root_available
        
        if process is None:
            return False
        
        print(f"[录屏] 正在停止录屏进程...")
        
        try:
            # 发送SIGINT优雅停止
            if use_root:
                self.run_shell('pkill -2 screenrecord', use_root=True, timeout=5)
            else:
                self.run(['shell', 'pkill', '-2', 'screenrecord'], timeout=5)
            time.sleep(3)
            
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=2)
        except Exception as e:
            print(f"[警告] 停止录屏进程时出错: {e}")
            try:
                process.kill()
                process.wait(timeout=1)
            except:
                pass
        
        # 确保所有screenrecord进程都被终止
        if use_root:
            self.run_shell('pkill -9 screenrecord', use_root=True)
        else:
            self.run(['shell', 'pkill', '-9', 'screenrecord'])
        
        print(f"[录屏] 等待文件写入完成...")
        time.sleep(3)
        
        last_size = 0
        stable_count = 0
        for i in range(5):
            success, stdout, _ = self.run_shell(f'ls -la {save_path}', use_root=use_root)
            if success:
                parts = stdout.split()
                if len(parts) >= 5:
                    try:
                        current_size = int(parts[4])
                        print(f"[录屏] 文件大小检查 {i+1}/5: {current_size} bytes")
                        if current_size == last_size and current_size > 0:
                            stable_count += 1
                            if stable_count >= 2:
                                print(f"[录屏] 文件已稳定: {current_size} bytes")
                                return True
                        last_size = current_size
                    except:
                        pass
            time.sleep(1)
        
        print(f"[警告] 等待文件稳定超时，但文件可能可用")
        return True
    
    @staticmethod
    def find_scrcpy() -> Optional[str]:
        """Find the platform-native scrcpy executable locally or on PATH."""
        executable_name = 'scrcpy.exe' if sys.platform == 'win32' else 'scrcpy'
        # 本地相对路径
        local_dirs = [
            Path('scrcpy'),
            Path('scrcpy-win64'),
            Path(__file__).parent / 'scrcpy',
            Path(__file__).parent / 'scrcpy-win64',
        ]
        for d in local_dirs:
            candidate = d / executable_name
            if candidate.is_file() and (
                sys.platform == 'win32' or os.access(candidate, os.X_OK)
            ):
                return str(candidate)

        # shutil.which applies the current platform's PATH/PATHEXT rules.
        return shutil.which(executable_name)
    
    def start_scrcpy_record(self, save_path: str, duration: int,
                            device_id: Optional[str] = None) -> Optional[subprocess.Popen]:
        """使用 scrcpy 录制屏幕，无 180 秒限制"""
        scrcpy_path = self.find_scrcpy()
        if not scrcpy_path:
            print("[警告] 未找到 scrcpy，无法使用 scrcpy 录屏")
            return None
        
        Path(save_path).parent.mkdir(parents=True, exist_ok=True)
        
        target_device_id = device_id or getattr(self, 'device_id', None)
        cmd = [
            scrcpy_path,
            '--record', save_path,
            '--no-playback',
            '--no-control',
        ]
        if sys.platform == 'win32':
            # On Windows, record to Matroska so CTRL_BREAK cannot leave an
            # unfinalized MP4 moov atom. The file is remuxed after stopping.
            cmd.extend(['--record-format', 'mkv'])
        if target_device_id:
            cmd.extend(['-s', target_device_id])
        # scrcpy 的 --time-limit 以秒为单位，设置后自动停止
        if duration and duration > 0:
            cmd.extend(['--time-limit', str(duration)])
        
        print(f"[录屏] 使用 scrcpy 启动录屏: {save_path}")
        try:
            recorder_env = dict(os.environ)
            # scrcpy treats ADB as an executable path. Some Windows setups set
            # it to the platform-tools directory, which makes CreateProcessW
            # fail with error 5. Prefer the adb.exe bundled beside scrcpy.
            if sys.platform == 'win32':
                bundled_adb = Path(scrcpy_path).resolve().parent / "adb.exe"
                if bundled_adb.is_file():
                    recorder_env["ADB"] = str(bundled_adb)
            # Windows 上需要创建新进程组，以便后续能优雅终止
            creationflags = 0
            if sys.platform == 'win32':
                creationflags = subprocess.CREATE_NEW_PROCESS_GROUP
            
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding='utf-8',
                errors='replace',
                creationflags=creationflags,
                env=recorder_env,
            )
            # Keep the recorder's own deadline. At normal task completion we
            # wait for this self-managed exit instead of terminating scrcpy.
            process._scrcpy_natural_deadline = (
                time.monotonic() + duration if duration and duration > 0 else None
            )
            
            # 等待 scrcpy 启动并检查是否立即退出
            time.sleep(2)
            if process.poll() is not None:
                stderr = process.stderr.read() if process.stderr else ""
                print(f"[警告] scrcpy 启动失败: {stderr}")
                return None
            
            print("[录屏] scrcpy 录屏已启动")
            return process
        except Exception as e:
            print(f"[警告] 启动 scrcpy 失败: {e}")
            return None
    
    def _stop_scrcpy_record_legacy(self, process: subprocess.Popen,
                           save_path: str = '') -> bool:
        """停止 scrcpy 录屏"""
        if process is None:
            return False
        
        print("[录屏] 正在停止 scrcpy 录屏...")
        try:
            if sys.platform == 'win32':
                # 先尝试优雅终止
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=2)
            else:
                import signal
                process.send_signal(signal.SIGINT)
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.terminate()
                    process.wait(timeout=2)
        except Exception as e:
            print(f"[警告] 停止 scrcpy 时出错: {e}")
            try:
                process.kill()
            except:
                pass
        
        # 等待文件写入完成
        if save_path and Path(save_path).exists():
            print("[录屏] 等待 scrcpy 文件写入完成...")
            last_size = 0
            for i in range(10):
                current_size = Path(save_path).stat().st_size
                if current_size == last_size and current_size > 0:
                    print(f"[录屏] scrcpy 文件已稳定: {current_size} bytes")
                    return True
                last_size = current_size
                time.sleep(1)
            print("[录屏] scrcpy 文件写入完成")
            return True
        
        return True
    
    def stop_scrcpy_record(self, process: subprocess.Popen,
                           save_path: str = '',
                           final_mp4_path: str = '') -> bool:
        """Stop scrcpy using the platform-appropriate container workflow."""
        if sys.platform != 'win32':
            return self._stop_scrcpy_record_posix(process, save_path)

        return self._stop_scrcpy_record_windows(
            process,
            save_path,
            final_mp4_path,
        )

    def _stop_scrcpy_record_posix(self, process: subprocess.Popen,
                                  save_path: str = '') -> bool:
        """Let scrcpy finalize its MP4 directly on macOS and Linux."""
        if process is None:
            return False

        print("[Recording] finalizing scrcpy MP4...")
        try:
            if process.poll() is None:
                deadline = getattr(process, "_scrcpy_natural_deadline", None)
                remaining = deadline - time.monotonic() if deadline else None
                if remaining is not None and remaining <= 60:
                    print(
                        "[Recording] waiting for scrcpy's natural MP4 finalization "
                        f"({max(0.0, remaining):.1f}s remaining)..."
                    )
                    process.wait()

            if process.poll() is None:
                print("[Recording] sending SIGINT; waiting for MP4 finalization...")
                process.send_signal(signal.SIGINT)
                try:
                    process.wait(timeout=90)
                    print("[Recording] scrcpy exited gracefully")
                except subprocess.TimeoutExpired:
                    print(
                        "[Recording] graceful stop timed out after 90s; "
                        "forcing termination..."
                    )
                    process.terminate()
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        print(
                            "[Recording] forced kill required; the MP4 may be invalid."
                        )
                        process.kill()
                        process.wait(timeout=5)
        except Exception as exc:
            print(f"[Recording] error while stopping scrcpy: {exc}")
            try:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=2)
            except Exception:
                pass

        if not save_path or not Path(save_path).exists():
            print(f"[Recording] ERROR: recording file is missing: {save_path}")
            return False

        size = Path(save_path).stat().st_size
        if size <= 0:
            print(f"[Recording] ERROR: recording file is empty: {save_path}")
            return False

        print(f"[Recording] scrcpy exited; validating MP4 ({size} bytes)...")
        return self._validate_video_file(Path(save_path), label="MP4")

    def _stop_scrcpy_record_windows(self, process: subprocess.Popen,
                                    save_path: str = '',
                                    final_mp4_path: str = '') -> bool:
        """Stop Windows scrcpy, validate its MKV, then remux to MP4."""
        if process is None:
            return False

        print("[Recording] finalizing scrcpy recording...")
        try:
            if process.poll() is None:
                # CTRL_BREAK can discard the currently open Matroska cluster.
                # Keep a short post-roll so the requested business endpoint is
                # already in a closed cluster before interrupting scrcpy.
                print("[Recording] keeping 5s MKV post-roll before stopping...")
                time.sleep(5)
            if process.poll() is None:
                print("[Recording] sending CTRL_BREAK_EVENT; finalizing MKV...")
                process.send_signal(signal.CTRL_BREAK_EVENT)
                try:
                    exit_code = process.wait(timeout=30)
                    print(f"[Recording] scrcpy stopped with code {exit_code}")
                except subprocess.TimeoutExpired:
                    print("[Recording] stop signal timed out after 30s; forcing termination...")
                    process.terminate()
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        print("[Recording] forced kill required; preserving the MKV for recovery.")
                        process.kill()
                        process.wait(timeout=5)
        except Exception as exc:
            print(f"[Recording] error while stopping scrcpy: {exc}")
            try:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=2)
            except Exception:
                pass

        exit_code = process.poll()
        if exit_code not in (None, 0):
            stderr_text = ""
            try:
                stderr_text = process.stderr.read().strip() if process.stderr else ""
            except Exception:
                pass
            print(f"[Recording] scrcpy exit code: {exit_code}")
            if stderr_text:
                stderr_lines = stderr_text.splitlines()
                print("[Recording] scrcpy stderr (last 20 lines):")
                print("\n".join(stderr_lines[-20:]))

        if not save_path or not Path(save_path).exists():
            print(f"[Recording] ERROR: recording file is missing: {save_path}")
            return False

        size = Path(save_path).stat().st_size
        if size <= 0:
            print(f"[Recording] ERROR: recording file is empty: {save_path}")
            return False

        print(f"[Recording] scrcpy exited; validating MKV ({size} bytes)...")
        if not self._validate_video_file(Path(save_path), label="MKV"):
            print("[Recording] ERROR: the temporary MKV is unreadable; preserving it.")
            return False
        if not final_mp4_path:
            print("[Recording] ERROR: final MP4 path is missing; preserving the MKV.")
            return False
        return self._remux_video_to_mp4(Path(save_path), Path(final_mp4_path))

    @staticmethod
    def _validate_video_file(video_path: Path, *, label: str = "video") -> bool:
        """Check that ffprobe can parse a recorded video and its duration."""
        ffprobe = shutil.which("ffprobe")
        if not ffprobe:
            print(f"[Recording] WARNING: ffprobe is unavailable; {label} was not verified.")
            return True

        try:
            result = subprocess.run(
                [
                    ffprobe,
                    "-v", "error",
                    "-select_streams", "v:0",
                    "-show_entries", "stream=codec_type:format=duration",
                    "-of", "default=noprint_wrappers=1:nokey=1",
                    str(video_path),
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=20,
            )
        except Exception as exc:
            print(f"[Recording] ERROR: ffprobe validation could not run: {exc}")
            return False

        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip().replace("\n", " ")
            print(f"[Recording] ERROR: invalid {label} container: {detail}")
            return False

        values = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        if "video" not in values:
            print(f"[Recording] ERROR: {label} contains no readable video stream.")
            return False
        if label.upper() == "MKV":
            # An interrupted Matroska file may intentionally have no container
            # duration/cues while its clusters remain fully remuxable.
            print("[Recording] MKV validation passed: readable video stream found")
            return True

        try:
            duration = next(float(value) for value in values if value != "video")
        except (StopIteration, ValueError):
            print("[Recording] ERROR: ffprobe returned no usable duration.")
            return False

        if duration <= 0:
            print("[Recording] ERROR: MP4 duration is zero.")
            return False

        print(f"[Recording] {label} validation passed: {duration:.3f}s")
        return True

    def _remux_video_to_mp4(self, source_path: Path, final_mp4_path: Path) -> bool:
        """Losslessly remux a validated scrcpy MKV into the required MP4."""
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            print("[Recording] ERROR: ffmpeg is unavailable; preserving the MKV.")
            return False

        temp_mp4 = final_mp4_path.with_name(
            f"{final_mp4_path.stem}.tmp{final_mp4_path.suffix}"
        )
        try:
            if temp_mp4.exists():
                temp_mp4.unlink()
            print(f"[Recording] remuxing MKV to MP4: {final_mp4_path}")
            result = subprocess.run(
                [
                    ffmpeg,
                    "-hide_banner",
                    "-y",
                    "-i", str(source_path),
                    "-c", "copy",
                    "-movflags", "+faststart",
                    "-f", "mp4",
                    str(temp_mp4),
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=120,
                check=False,
            )
            if result.returncode != 0:
                detail_lines = (result.stderr or "").strip().splitlines()
                detail = detail_lines[-1] if detail_lines else "unknown ffmpeg error"
                print(f"[Recording] ERROR: MP4 remux failed: {detail}")
                return False
            temp_mp4.replace(final_mp4_path)
            if not self._validate_video_file(final_mp4_path, label="MP4"):
                print("[Recording] ERROR: remuxed MP4 is invalid; preserving the MKV.")
                return False
            source_path.unlink()
            print("[Recording] MP4 remux completed; temporary MKV removed")
            return True
        except Exception as exc:
            print(f"[Recording] ERROR: MP4 remux failed; preserving the MKV: {exc}")
            return False

    def pull_file(self, remote: str, local: str) -> bool:
        """拉取文件"""
        success, stdout, stderr = self.run(['pull', remote, local], timeout=60)
        if not success:
            error_msg = stderr.strip() if stderr else "未知错误"
            print(f"[警告] 拉取文件失败 {remote}: {error_msg}")
        return success
    
    def start_logcat_stream(self, output_file: str) -> Optional[subprocess.Popen]:
        """Start a live logcat stream so the log only covers this capture window."""
        try:
            Path(output_file).parent.mkdir(parents=True, exist_ok=True)
            log_handle = open(output_file, 'w', encoding='utf-8', errors='replace')
            since = self._logcat_since_time()
            log_handle.write(f"# logcat stream started at {datetime.now().isoformat(timespec='seconds')}\n")
            log_handle.write(f"# logcat since {since} (capture start minus 120 seconds)\n")
            log_handle.flush()
            proc = subprocess.Popen(
                self.base_cmd + ['logcat', '-v', 'threadtime', '-T', since],
                stdout=log_handle,
                stderr=subprocess.STDOUT,
            )
            proc._unicapture_log_handle = log_handle
            print(f"[日志] logcat实时流已启动: {output_file}")
            return proc
        except Exception as e:
            print(f"[警告] logcat实时流启动失败: {e}")
            try:
                if 'log_handle' in locals():
                    log_handle.close()
            except Exception:
                pass
            return None

    def stop_logcat_stream(self, proc: Optional[subprocess.Popen], timeout: int = 10) -> bool:
        """Stop a live logcat stream and close its output file."""
        if not proc:
            return False
        success = True
        try:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=timeout)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=timeout)
        except Exception as e:
            print(f"[警告] 停止logcat实时流失败: {e}")
            success = False
        finally:
            log_handle = getattr(proc, '_unicapture_log_handle', None)
            if log_handle:
                try:
                    log_handle.write(f"# logcat stream stopped at {datetime.now().isoformat(timespec='seconds')}\n")
                    log_handle.close()
                except Exception:
                    success = False
        return success

    def get_logcat(self, output_file: str) -> bool:
        """Fallback: dump the current system log buffer."""
        since = self._logcat_since_time()
        success, stdout, stderr = self.run(['logcat', '-d', '-T', since], timeout=30)
        if success and stdout is not None:
            try:
                with open(output_file, 'w', encoding='utf-8', errors='replace') as f:
                    f.write(f"# fallback logcat dump at {datetime.now().isoformat(timespec='seconds')}\n")
                    f.write(f"# logcat since {since} (capture start minus 120 seconds)\n")
                    f.write(stdout)
                return True
            except Exception as e:
                print(f"[警告] 写入日志文件失败: {e}")
                return False
        else:
            try:
                with open(output_file, 'w', encoding='utf-8') as f:
                    if stderr:
                        f.write(f"# 日志获取失败: {stderr}\n")
                    else:
                        f.write("# 日志获取失败: 无法读取 logcat\n")
            except Exception:
                pass
            return False

    @staticmethod
    def _logcat_since_time() -> str:
        """Return a logcat -T timestamp anchored two minutes before now."""
        return (datetime.now() - timedelta(seconds=120)).strftime('%m-%d %H:%M:%S.000')
    
    def check_tcpdump(self) -> bool:
        """检查设备是否有tcpdump"""
        success, _, _ = self.run(['shell', 'which', 'tcpdump'])
        if success:
            return True
        for path in ['/system/bin/tcpdump', '/system/xbin/tcpdump', '/data/local/tmp/tcpdump']:
            success, _, _ = self.run(['shell', 'test', '-f', path])
            if success:
                return True
        return False
    
    def start_tcpdump(self, save_path: str = '/data/local/tmp/capture.pcap', 
                      app_package: str = None, device_ips: list = None,
                      pcap_mode: str = 'standard') -> Optional[subprocess.Popen]:
        """开始抓包（root模式/tcpdump）"""
        tcpdump_path = 'tcpdump'
        for path in ['/system/bin/tcpdump', '/system/xbin/tcpdump', '/data/local/tmp/tcpdump']:
            success, _, _ = self.run(['shell', 'test', '-f', path])
            if success:
                tcpdump_path = path
                break
        
        if pcap_mode == 'minimal':
            snaplen = 64
            filter_str = 'tcp port 80 or tcp port 443 or tcp port 8080 or tcp port 1935 or udp port 3478'
        elif pcap_mode == 'standard':
            snaplen = 96
            filter_parts = ['portrange 1-65535']
            excluded_ports = [53, 123, 1900, 5353]
            for port in excluded_ports:
                filter_parts.append(f'and not port {port}')
            filter_parts.append('and not host 127.0.0.1')
            filter_str = ' '.join(filter_parts)
        else:
            snaplen = 0
            filter_str = ''
        
        tcpdump_args = [tcpdump_path, '-i', 'any', '-s', str(snaplen), '-w', save_path]
        if filter_str:
            tcpdump_args.append(filter_str)
        
        inner_cmd = ' '.join(tcpdump_args)
        su_cmd = f'su -c "{inner_cmd}"'
        cmd = self.base_cmd + ['shell', su_cmd]
        
        print(f"[抓包] 模式: {pcap_mode} (root/tcpdump)")
        if pcap_mode == 'minimal':
            print(f"[抓包] 仅抓业务端口，包截断:{snaplen}字节")
        elif pcap_mode == 'standard':
            print(f"[抓包] 过滤DNS/mDNS等，包截断:{snaplen}字节")
        else:
            print(f"[抓包] 全量抓包，不截断")
        if filter_str:
            print(f"[抓包] 过滤规则: {filter_str}")
        
        try:
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding='utf-8',
                errors='replace'
            )
            time.sleep(1)
            if process.poll() is None:
                return process
            else:
                stderr = process.stderr.read() if process.stderr else ""
                print(f"[警告] tcpdump启动失败: {stderr}")
                return None
        except Exception as e:
            print(f"[警告] tcpdump启动异常: {e}")
            return None
    
    def stop_tcpdump(self, process: subprocess.Popen) -> bool:
        """停止抓包（root模式）"""
        if process is None:
            return False
        try:
            process.terminate()
            process.wait(timeout=3)
            return True
        except:
            try:
                process.kill()
                process.wait(timeout=2)
            except:
                pass
            self.run_shell('pkill -9 tcpdump', use_root=True, timeout=5)
            return True


class AppCollector:
    """APP采集器，支持root和无root两种模式"""
    
    def __init__(
        self,
        app_config: AppConfig,
        scene: str,
        device_id: Optional[str] = None,
        duration: int = 600,
        target_business_duration: Optional[int] = None,
        resolution: Optional[str] = None,
        output_dir: str = './data',
        location: str = 'default',
        pcap_mode: str = 'standard',
        capture_mode: str = 'auto',
        pcap_app_package: Optional[str] = None,
        pcapdroid_api_key: Optional[str] = None,
        enable_qoe: bool = True,
        external_control: bool = False,
        clear_app: bool = False,
        enable_postprocess: bool = True,
        enable_flow_labeling: bool = True,
        postprocess_enable_vlm: bool = True,
        postprocess_vlm_every: int = 5,
        postprocess_vlm_max_calls: int = 20,
        postprocess_ocr_every: int = 1,
    ):
        self.app_config = app_config
        self.scene_config = app_config.get_scene(scene)
        if not self.scene_config:
            raise ValueError(f"场景 '{scene}' 不存在于应用 '{app_config.name}'")
        
        self.device_id = device_id
        self.duration = duration or self.scene_config.duration
        self.target_business_duration = target_business_duration
        self.resolution = resolution
        self.output_dir = Path(output_dir)
        self.location = location
        self.pcap_mode = pcap_mode
        self.capture_mode = capture_mode  # 'auto', 'root', 'noroot'
        self.pcap_app_package = pcap_app_package  # 指定 PCAPdroid app_filter 目标应用包名
        self.pcapdroid_api_key = pcapdroid_api_key
        self.clear_app = clear_app
        # 根据APP类型和场景判断是否需要QoE标注
        self.qoe_applicable = is_qoe_applicable(self.app_config, scene)
        self.enable_qoe = enable_qoe and QOE_AVAILABLE and self.qoe_applicable
        self.external_control = external_control
        self.enable_postprocess = enable_postprocess
        self.enable_flow_labeling = enable_flow_labeling
        self.postprocess_enable_vlm = postprocess_enable_vlm
        self.postprocess_vlm_every = max(1, postprocess_vlm_every)
        self.postprocess_vlm_max_calls = max(1, postprocess_vlm_max_calls)
        self.postprocess_ocr_every = max(1, postprocess_ocr_every)
        self.postprocess_summary: Optional[Dict] = None
        
        self.adb = ADBHelper(device_id)
        self.running = False
        
        # 采集数据
        self.start_time: Optional[datetime] = None
        self.end_time: Optional[datetime] = None
        self.video_start_time: Optional[datetime] = None
        self.video_end_time: Optional[datetime] = None
        self.device_info: Dict = {}
        self.pcap_file: Optional[str] = None
        self.video_file: Optional[str] = None
        self.recording_valid: Optional[bool] = None
        self.log_file: Optional[str] = None
        self.yaml_file: Optional[str] = None
        self.qoe_file: Optional[str] = None
        self.sa_file: Optional[str] = None
        self.flow_labeled_file: Optional[str] = None
        
        # 子进程
        self.screenrecord_proc: Optional[subprocess.Popen] = None
        self.tcpdump_proc: Optional[subprocess.Popen] = None
        self.logcat_proc: Optional[subprocess.Popen] = None
        
        # QoE监测
        self.qoe_monitor = None
        self.qoe_thread = None
        
        # 是否抓包
        self.enable_pcap = True
        # 是否使用 scrcpy 录屏
        self.use_scrcpy = False
        self.scrcpy_video_path: Optional[str] = None
        self.scrcpy_record_path: Optional[str] = None
        
        # 根据模式设置设备上的文件路径
        self.use_root = False  # 最终是否使用root，在check_env中确定
        self._setup_device_paths()
        
        # 生成文件前缀
        self._generate_prefix()
    
    def _setup_device_paths(self):
        """根据抓包模式设置设备上的文件路径"""
        # 路径将在check_env中根据实际模式最终确定
        # root模式使用 /data/local/tmp/ (需要root读写)
        # 无root模式使用 /sdcard/Download/pcap_collector/ (无需root)
        self.root_pcap_path = '/data/local/tmp/capture.pcap'
        self.root_screen_path = '/data/local/tmp/screenrecord.mp4'
        self.noroot_base_path = '/sdcard/Download/pcap_collector'
        self.noroot_pcap_path = f'{self.noroot_base_path}/capture.pcap'
        self.noroot_screen_path = f'{self.noroot_base_path}/screenrecord.mp4'
        
        # 运行时实际使用的路径
        self.pcap_device_path = self.root_pcap_path
        self.screenrecord_device_path = self.root_screen_path
    
    def _generate_prefix(self):
        """生成文件名前缀和样本目录"""
        device_short = self.device_id[:8] if self.device_id else 'unknown'
        resolution_short = self.resolution.replace(' ', '') if self.resolution else 'auto'
        if not hasattr(self, '_capture_timestamp'):
            self._capture_timestamp = datetime.now().strftime('%Y%m%dT%H%M%S')
        timestamp = self._capture_timestamp
        
        naming_duration = self.target_business_duration or self.duration
        self.prefix = f"{self.app_config.name}_{device_short}_{self.scene_config.name}_{resolution_short}_{naming_duration}_{timestamp}"
        # 每个样本生成独立目录：应用名-采集时长-时间戳
        self.sample_dir = self.output_dir / f"{self.app_config.name}-{naming_duration}-{timestamp}"
        self.sample_dir.mkdir(parents=True, exist_ok=True)
    
    def _determine_capture_mode(self) -> str:
        """确定实际使用的抓包模式"""
        if self.capture_mode == 'root':
            if not self.adb.root_available:
                print("[警告] 强制root模式但设备无root权限，将尝试无root模式")
                return 'noroot'
            return 'root'
        elif self.capture_mode == 'noroot':
            return 'noroot'
        else:  # auto
            if self.adb.root_available and self.adb.check_tcpdump():
                print("[模式] 检测到root权限和tcpdump，使用root模式")
                return 'root'
            else:
                print("[模式] 未检测到root/tcumpdump，使用无root模式")
                return 'noroot'
    
    def check_env(self) -> bool:
        """检查环境"""
        print("[检查] 检查设备连接...")
        if not self.adb.check_connection():
            print(f"[错误] {self.adb.connection_error or '设备未连接'}")
            print("已连接设备:")
            subprocess.run([
                os.getenv('MINI_PILOT_ADB_BIN')
                or os.getenv('ADB_BIN')
                or 'adb',
                'devices',
            ])
            return False
        if self.device_id != self.adb.device_id:
            self.device_id = self.adb.device_id
            self._generate_prefix()
        print(f"[检查] 设备已连接: {self.device_id}")
        
        # 获取设备信息
        self.device_info = self.adb.get_device_info()
        print(f"[检查] 设备信息: {self.device_info.get('brand')} {self.device_info.get('model')} Android {self.device_info.get('os_version')}")
        
        # 确定实际抓包模式
        actual_mode = self._determine_capture_mode()
        self.use_root = (actual_mode == 'root')
        
        if self.use_root:
            # root模式：检查tcpdump
            self.pcap_device_path = self.root_pcap_path
            self.screenrecord_device_path = self.root_screen_path
            
            if self.enable_pcap:
                if self.adb.check_tcpdump():
                    print("[检查] tcpdump已就绪")
                else:
                    print("[错误] 设备上未找到tcpdump，无法按要求启动抓包")
                    print("[提示] 可通过 'adb push tcpdump /data/local/tmp/' 安装tcpdump")
                    print("[提示] 如果确实不需要pcap，请显式使用 --no-pcap")
                    return False
        else:
            # 无root模式：检查抓包App
            self.pcap_device_path = self.noroot_pcap_path
            self.screenrecord_device_path = self.noroot_screen_path
            
            if self.enable_pcap:
                # 创建无root采集目录
                self.adb.run(['shell', 'mkdir', '-p', self.noroot_base_path])
                
                detected = self.adb.detect_pcap_app()
                if detected:
                    app_name, pkg = self.adb.get_detected_pcap_info()
                    print(f"[检查] 检测到无root抓包App: {app_name} ({pkg})")
                    if self.pcap_app_package:
                        print(f"[检查] PCAPdroid app_filter目标应用: {self.pcap_app_package}")
                else:
                    print("[错误] 未检测到无root抓包App，无法按要求启动抓包")
                    print("[提示] 请安装以下App之一:")
                    print("  - PCAPdroid (推荐): https://github.com/emanuele-f/PCAPdroid")
                    print("  - HttpCanary")
                    print("[提示] 或使用 --no-pcap 跳过抓包")
                    return False
        
        return True
    
    def prepare(self):
        """准备阶段"""
        print("\n[准备] 清理环境...")
        
        if not self.external_control:
            # 停止应用
            self.adb.stop_app(self.app_config.package)
            time.sleep(1)
            
            # 清理应用数据（默认不清理，避免清除登录信息）
            if self.clear_app:
                self.adb.clear_app(self.app_config.package)
                time.sleep(1)
            else:
                print("[准备] 保留应用数据（登录信息等），如需清理可添加 --clear-app")
        else:
            print("[准备] 外部控制模式：跳过停止/清理目标App，由执行器控制App状态")
        
        # 删除设备上的旧文件
        if self.use_root:
            self.adb.run_shell(f'rm -f {self.screenrecord_device_path}')
            self.adb.run_shell(f'rm -f {self.pcap_device_path}')
        else:
            self.adb.run(['shell', 'rm', '-f', self.screenrecord_device_path])
            self.adb.run(['shell', 'rm', '-f', self.pcap_device_path])
        
        print("[准备] 环境准备完成")
    
    def _run_qoe_monitor(self):
        """在后台线程中运行QoE监测"""
        try:
            if self.qoe_monitor:
                self.qoe_monitor.run()
        except Exception as e:
            print(f"\n[警告] QoE监测线程异常: {e}")
    
    def start_capture(self):
        """开始采集"""
        print("\n[采集] 启动采集...")
        self.sample_dir.mkdir(parents=True, exist_ok=True)

        log_path = self.sample_dir / f"{self.prefix}.log"
        self.logcat_proc = self.adb.start_logcat_stream(str(log_path))
        if self.logcat_proc:
            self.log_file = str(log_path)
        else:
            print("[警告] logcat实时流未启动，结束收集时将尝试fallback dump")
        
        # 开始抓包（先启动抓包，确保捕获完整流量）
        if self.enable_pcap:
            print("[采集] 开始抓包...")
            if self.use_root:
                # root模式：使用tcpdump
                device_ips = self.device_info.get('ipv4', []) + self.device_info.get('ipv6', [])
                self.tcpdump_proc = self.adb.start_tcpdump(
                    self.pcap_device_path,
                    app_package=self.app_config.package,
                    device_ips=device_ips,
                    pcap_mode=self.pcap_mode
                )
                if self.tcpdump_proc:
                    print("[采集] 抓包已启动 (tcpdump)")
                else:
                    raise RuntimeError(
                        "tcpdump抓包启动失败；本轮采集已终止。"
                        "如果确实不需要pcap，请显式使用 --no-pcap"
                    )
                time.sleep(1)
            else:
                # 无root模式：自动启动抓包
                pkg = self.adb._detected_pcap_package
                app_name = self.adb._detected_pcap_app
                if pkg:
                    if app_name == 'pcapdroid':
                        # PCAPdroid：使用Intent API全自动控制
                        pcap_output_name = f"{self.prefix}.pcap"
                        app_filter_package = self.pcap_app_package or self.app_config.package
                        success = self.adb.start_pcapdroid_capture(
                            output_name=pcap_output_name,
                            app_package=app_filter_package,
                            api_key=self.pcapdroid_api_key,
                            pcapdroid_package=pkg
                        )
                        if success:
                            print("[采集] PCAPdroid自动启动成功")
                        else:
                            raise RuntimeError(
                                f"PCAPdroid启动失败 ({pkg})；本轮采集已终止。"
                                "如果确实不需要pcap，请显式使用 --no-pcap"
                            )
                    else:
                        # 其他App：fallback到手动模式
                        success = self.adb.start_pcap_app(pkg, output_hint=self.pcap_device_path)
                        if success:
                            print("[采集] 用户已确认抓包App启动")
                        else:
                            raise RuntimeError(
                                f"抓包App启动失败 ({pkg})；本轮采集已终止。"
                                "如果确实不需要pcap，请显式使用 --no-pcap"
                            )
                else:
                    raise RuntimeError(
                        "无可用抓包App；本轮采集已终止。"
                        "如果确实不需要pcap，请显式使用 --no-pcap"
                    )
        
        # 开始录屏
        print("[采集] 开始录屏...")
        recorder_launch_time = datetime.now()
        scrcpy_path = self.adb.find_scrcpy()
        if scrcpy_path:
            # 使用 scrcpy 录屏，无 180 秒限制
            self.use_scrcpy = True
            video_path = self.sample_dir / f"{self.prefix}.mp4"
            record_path = (
                self.sample_dir / f"{self.prefix}.scrcpy.mkv"
                if sys.platform == 'win32'
                else video_path
            )
            self.scrcpy_video_path = str(video_path)
            self.scrcpy_record_path = str(record_path)
            print(f"[录屏] 检测到 scrcpy: {scrcpy_path}")
            self.screenrecord_proc = self.adb.start_scrcpy_record(
                self.scrcpy_record_path,
                self.duration,
                device_id=self.device_id
            )
        else:
            # fallback 到 screenrecord（仅支持 180 秒）
            self.use_scrcpy = False
            if self.duration > 180:
                print("[警告] 采集时长超过 180 秒，但系统未安装 scrcpy，screenrecord 会自动截断")
            self.screenrecord_proc = self.adb.start_screenrecord(
                self.screenrecord_device_path, 
                self.duration,
                use_root=self.use_root
            )
        if self.screenrecord_proc:
            self.video_start_time = recorder_launch_time
            print("[采集] 录屏已启动")
        else:
            print("[警告] 录屏启动失败，继续采集但不包含视频")
        
        # 启动QoE实时监测（后台线程）
        if self.enable_qoe:
            print("[采集] 启动QoE实时监测...")
            pcap_name = build_pcap_filename(self.prefix)
            if self.pcap_file and self.pcap_file.endswith('.pcapng'):
                pcap_name = build_pcap_filename(self.prefix, '.pcapng')
            
            qoe_csv_path = self.sample_dir / f"{self.prefix}_qoe.csv"
            
            # 根据APP类型自动调整卡顿阈值
            threshold = 92.0 if self.app_config.app_type == 'video' else 95.0
            stall_window = 3
            if self.app_config.name == 'honorofkings':
                threshold = 99.2
                stall_window = 4
            
            try:
                self.qoe_monitor = qm_module.QoEMonitor(
                    device_id=self.device_id,
                    interval=1.0,
                    frozen_threshold=threshold,
                    pcap_name=pcap_name,
                    app_type=self.app_config.app_type,
                    app_name=self.app_config.name,
                    scene=self.scene_config.name,
                    warmup_seconds=10,
                    stall_window=stall_window,
                    register_signal=False,  # 由主线程处理信号
                    csv_path=str(qoe_csv_path)
                )
                self.qoe_thread = threading.Thread(target=self._run_qoe_monitor, daemon=True)
                self.qoe_thread.start()
                time.sleep(0.5)
                print("[采集] QoE监测已启动（后台线程）")
            except Exception as e:
                print(f"[警告] QoE监测启动失败: {e}")
                self.qoe_monitor = None
                self.qoe_thread = None
        elif not self.qoe_applicable:
            print("[采集] QoE标注不适用（当前APP/场景不需要QoE标注）")
        
        if not self.external_control:
            # 启动应用
            print(f"[采集] 启动应用: {self.app_config.package}")
            self.adb.start_app(self.app_config.package)
            time.sleep(3)
        else:
            print("[采集] 外部控制模式：采集已启动，目标App和UI动作由执行器控制")
        
        # 记录开始时间
        self.start_time = datetime.now()
        self.running = True
        
        print(f"[采集] 开始执行场景: {self.scene_config.description}")
        print(f"[采集] 采集时长: {self.duration}秒")
    
    def execute_scene(self):
        """执行拨测场景（目前仅等待指定时长，可扩展UI自动化）"""
        remaining = self.duration
        while remaining > 0 and self.running:
            print(f"\r[采集] 剩余时间: {remaining}秒", end='', flush=True)
            time.sleep(1)
            remaining -= 1
        print()

    def write_external_ready(self, ready_file: Optional[str] = None):
        """通知外部执行器：采集侧初始化已完成，可以开始业务动作。"""
        if not ready_file:
            return
        ready_path = Path(ready_file)
        ready_path.parent.mkdir(parents=True, exist_ok=True)
        ready_path.write_text(
            f"ready at {datetime.now().isoformat(timespec='seconds')}\n",
            encoding='utf-8',
        )
        print(f"[采集] 外部控制模式：已写入就绪文件 {ready_path}")

    def wait_for_external_stop(self, stop_file: Optional[str] = None):
        """等待外部执行器通知停止采集。"""
        print("[采集] 外部控制模式：等待执行器完成任务...")
        stop_path = Path(stop_file) if stop_file else None
        while self.running:
            if stop_path and stop_path.exists():
                print(f"\n[采集] 检测到停止文件: {stop_path}")
                break
            time.sleep(0.5)
    
    def stop_capture(self):
        """停止采集"""
        print("\n[采集] 停止采集...")
        self.running = False
        
        # 记录结束时间
        self.end_time = datetime.now()
        
        # 停止录屏 - 先停止录屏再停止抓包，确保视频完整
        if self.screenrecord_proc:
            print("[采集] 停止录屏...")
            if self.use_scrcpy:
                self.recording_valid = self.adb.stop_scrcpy_record(
                    self.screenrecord_proc,
                    self.scrcpy_record_path or '',
                    final_mp4_path=self.scrcpy_video_path or '',
                )
            else:
                self.recording_valid = self.adb.stop_screenrecord(
                    self.screenrecord_proc,
                    self.screenrecord_device_path,
                    use_root=self.use_root,
                )
            print("[采集] 录屏已停止")
        
        # 停止QoE监测
        if self.qoe_monitor:
            print("[采集] 停止QoE监测...")
            self.qoe_monitor.running = False
            if self.qoe_thread and self.qoe_thread.is_alive():
                self.qoe_thread.join(timeout=15)
            print("[采集] QoE监测已停止")
        
        # 停止抓包
        if self.enable_pcap:
            if self.use_root:
                if self.tcpdump_proc:
                    print("[采集] 停止抓包 (tcpdump)...")
                    self.adb.stop_tcpdump(self.tcpdump_proc)
                    time.sleep(1)
                    # 修改权限以便拉取
                    self.adb.run_shell(f'chmod 644 {self.pcap_device_path}', use_root=True)
            else:
                # 无root模式：自动停止抓包
                pkg = self.adb._detected_pcap_package
                app_name = self.adb._detected_pcap_app
                if pkg:
                    print("[采集] 停止抓包App...")
                    if app_name == 'pcapdroid':
                        self.adb.stop_pcapdroid_capture(
                            api_key=self.pcapdroid_api_key,
                            pcapdroid_package=pkg
                        )
                    else:
                        self.adb.stop_pcap_app(pkg)
        
        if not self.external_control:
            # 停止应用
            self.adb.stop_app(self.app_config.package)
        else:
            print("[采集] 外部控制模式：跳过停止目标App")

        if self.logcat_proc:
            print("[采集] 停止logcat实时流...")
            self.adb.stop_logcat_stream(self.logcat_proc)
            self.logcat_proc = None
            print("[采集] logcat实时流已停止")
    
    def collect_files(self):
        """收集文件"""
        print("\n[收集] 拉取采集文件...")
        
        # 拉取录屏文件
        if self.screenrecord_proc:
            if self.use_scrcpy:
                # scrcpy 已直接录制到本地，无需从设备拉取
                if (
                    self.recording_valid is not False
                    and self.scrcpy_video_path
                    and Path(self.scrcpy_video_path).exists()
                ):
                    self.video_file = self.scrcpy_video_path
                    print(f"[收集] 录屏文件: {self.video_file}")
                elif (
                    self.scrcpy_record_path
                    and self.scrcpy_record_path != self.scrcpy_video_path
                    and Path(self.scrcpy_record_path).exists()
                    and self.adb._validate_video_file(
                        Path(self.scrcpy_record_path), label="MKV"
                    )
                ):
                    print(
                        "[警告] 最终MP4不可用，已保留可恢复的scrcpy MKV: "
                        f"{self.scrcpy_record_path}"
                    )
                else:
                    print(f"[警告] scrcpy 录屏文件不可用: {self.scrcpy_video_path}")
            else:
                video_path = self.sample_dir / f"{self.prefix}.mp4"
                remote_path = self.screenrecord_device_path
                
                if self.use_root:
                    self.adb.run_shell(f'chmod 644 {remote_path}', use_root=True)
                
                print(f"[收集] 拉取录屏: {remote_path}")
                if self.adb.pull_file(remote_path, str(video_path)):
                    self.video_file = str(video_path)
                    print(f"[收集] 录屏文件: {video_path}")
                else:
                    print(f"[警告] 录屏文件拉取失败")
                
                # 清理设备上的文件
                if self.use_root:
                    self.adb.run_shell(f'rm -f {remote_path}', use_root=True)
                else:
                    self.adb.run(['shell', 'rm', '-f', remote_path])
        else:
            print(f"[警告] 录屏未启动，跳过拉取")
        
        # 获取系统日志
        log_path = self.sample_dir / f"{self.prefix}.log"
        if self.log_file and Path(self.log_file).exists():
            print(f"[收集] 日志文件: {self.log_file}")
        elif self.adb.get_logcat(str(log_path)):
            self.log_file = str(log_path)
            print(f"[收集] 日志文件: {log_path}")
        
        # 拉取pcap文件
        if self.enable_pcap:
            pcap_path = self.sample_dir / build_pcap_filename(self.prefix)
            
            if self.use_root:
                # root模式：从固定路径拉取
                self.adb.run_shell(f'chmod 644 {self.pcap_device_path}', use_root=True)
                if self.adb.pull_file(self.pcap_device_path, str(pcap_path)):
                    self.pcap_file = str(pcap_path)
                    print(f"[收集] pcap文件: {pcap_path}")
                else:
                    print(f"[警告] pcap文件拉取失败")
                self.adb.run_shell(f'rm -f {self.pcap_device_path}', use_root=True)
            else:
                # 无root模式：搜索设备上的pcap文件
                print("[收集] 搜索设备上的pcap文件...")
                pcap_files = self.adb.find_pcap_files()
                
                if pcap_files:
                    # 优先选择匹配当前样本前缀的 pcap，否则选最新的
                    target_pcap = f"{self.prefix}.pcap"
                    matching = [p for p in pcap_files if target_pcap in p]
                    if matching:
                        remote_pcap = matching[0]
                        print(f"[收集] 发现匹配当前样本的pcap文件: {remote_pcap}")
                    else:
                        remote_pcap = pcap_files[0]
                        print(f"[收集] 发现pcap文件: {remote_pcap}")
                    
                    # 检查是否是pcapng，如果是需要转换或重命名提示
                    if remote_pcap.endswith('.pcapng'):
                        pcap_path = self.sample_dir / build_pcap_filename(
                            self.prefix, '.pcapng'
                        )
                        print("[提示] 检测到PCAPNG格式，部分工具可能需要转换")
                    
                    if self.adb.pull_file(remote_pcap, str(pcap_path)):
                        self.pcap_file = str(pcap_path)
                        print(f"[收集] pcap文件: {pcap_path}")
                    else:
                        print(f"[警告] pcap文件拉取失败")
                else:
                    print("[警告] 未在设备上找到pcap文件")
                    print("[提示] 请确保在抓包App中已保存文件")
                    # 尝试从用户提示的路径拉取
                    if self.pcap_device_path and self.pcap_device_path != self.root_pcap_path:
                        print(f"[提示] 尝试从指定路径拉取: {self.pcap_device_path}")
                        if self.adb.pull_file(self.pcap_device_path, str(pcap_path)):
                            self.pcap_file = str(pcap_path)
                            print(f"[收集] pcap文件: {pcap_path}")
    
    def cleanup_device_files(self):
        """清理设备上的临时文件（在异常或中断时调用）"""
        print("\n[清理] 清理设备临时文件...")
        
        if self.use_root:
            if hasattr(self, 'screenrecord_device_path') and self.screenrecord_device_path:
                self.adb.run_shell(f'rm -f {self.screenrecord_device_path}', use_root=True)
            if hasattr(self, 'pcap_device_path') and self.pcap_device_path:
                self.adb.run_shell(f'rm -f {self.pcap_device_path}', use_root=True)
        else:
            if hasattr(self, 'screenrecord_device_path') and self.screenrecord_device_path:
                self.adb.run(['shell', 'rm', '-f', self.screenrecord_device_path])
            if hasattr(self, 'pcap_device_path') and self.pcap_device_path:
                self.adb.run(['shell', 'rm', '-f', self.pcap_device_path])
        
        print("[清理] 设备临时文件清理完成")
    
    def generate_metadata(self):
        """生成元数据文件（六种格式之一：yaml, qoe.csv, sa.csv）"""
        print("\n[生成] 生成元数据文件...")
        
        # 生成YAML
        yaml_data = generate_yaml(
            prefix=self.prefix,
            device_id=self.device_id,
            device_info=self.device_info,
            app_name=self.app_config.name,
            app_package=self.app_config.package,
            app_type=self.app_config.app_type,
            scene=self.scene_config.name,
            resolution=self.resolution,
            start_time=self.start_time,
            end_time=self.end_time,
            location=self.location,
            scene_params=self.scene_config.params,
            app_version=self.app_config.version,
        )
        yaml_data['packet_capture']['location'] = 'device' if self.pcap_file else 'upf'
        video_path = self.video_file or self.scrcpy_video_path
        probed_duration = probe_video_duration(video_path)
        if probed_duration is not None:
            video_start = self.video_start_time or self.start_time
            self.video_end_time = video_start + timedelta(seconds=probed_duration)
            yaml_data['video_record']['start_time'] = (
                video_start.strftime('%Y%m%dT%H%M%S%f')[:17] + '+0800'
            )
            yaml_data['video_record']['end_time'] = (
                self.video_end_time.strftime('%Y%m%dT%H%M%S%f')[:17] + '+0800'
            )
            yaml_data['video_record']['duration'] = round(probed_duration, 3)
            yaml_data['video_record']['duration_source'] = 'ffprobe'
        else:
            fallback_duration = max(
                1,
                int(round((self.end_time - self.start_time).total_seconds())),
            )
            yaml_data['video_record']['duration'] = fallback_duration
            yaml_data['video_record']['duration_source'] = 'collector_wall_clock_fallback'
            print(
                "[警告] 无法读取MP4真实时长，video_record.duration已回退到采集墙钟时间"
            )
        yaml_data['video_record']['record_format'] = 'mp4'
        if self.target_business_duration:
            yaml_data['task_info']['business_duration'] = self.target_business_duration
        
        yaml_path = self.sample_dir / f"{self.prefix}.yaml"
        with open(yaml_path, 'w', encoding='utf-8') as f:
            yaml.dump(yaml_data, f, allow_unicode=True, sort_keys=False)
        self.yaml_file = str(yaml_path)
        print(f"[生成] YAML文件: {yaml_path}")
        
        # 生成/保留 QoE CSV。实时 QoE 监测线程会先写入真实检测结果；
        # metadata 阶段只在没有真实文件时生成占位模板，避免覆盖真实标注。
        qoe_path = self.sample_dir / f"{self.prefix}_qoe.csv"
        if self._has_real_qoe_csv(qoe_path):
            self.qoe_file = str(qoe_path)
            print(f"[生成] 保留实时QoE文件: {qoe_path}")
        elif not self.qoe_applicable:
            self._generate_empty_qoe_csv(qoe_path)
            self.qoe_file = str(qoe_path)
            print(f"[生成] QoE标注不适用文件: {qoe_path}")
            print("[提示] 当前APP/场景不需要QoE标注，已生成仅含时间戳的空文件")
        else:
            self._generate_placeholder_qoe_csv(qoe_path)
            self.qoe_file = str(qoe_path)
            print(f"[生成] QoE占位文件: {qoe_path}")
            print("[提示] 未检测到实时QoE数据，已生成占位QoE数据")
        
        # 生成SA CSV
        sa_path = self.sample_dir / f"{self.prefix}_sa.csv"
        
        if self.pcap_file and SCAPY_AVAILABLE:
            print(f"[提示] pcap文件已采集: {self.pcap_file}")
            print("[生成] 正在从pcap解析生成SA数据...")
            sa_data = self.parse_pcap_to_sa(self.pcap_file)
            if sa_data and len(sa_data) > 1:
                with open(sa_path, 'w', newline='', encoding='utf-8') as f:
                    writer = csv.writer(f)
                    writer.writerows(sa_data)
                self.sa_file = str(sa_path)
                print(f"[生成] SA文件: {sa_path} ({len(sa_data)-1}条流)")
            else:
                print("[警告] pcap解析未获得有效数据，生成空SA模板")
                self._generate_empty_sa(sa_path)
        else:
            self._generate_empty_sa(sa_path)
            if self.pcap_file:
                print(f"[提示] 可使用Wireshark或tshark分析pcap生成SA标注")

    def run_postprocess(self):
        """Run logcat and video QoE annotations after every artifact is finalized."""
        if not self.enable_postprocess:
            print("[后处理] 已通过配置关闭")
            return
        print("\n[后处理] 开始应用时间和秒级QoE标注...")
        try:
            from postprocess_runner import run_sample_postprocess

            self.postprocess_summary = run_sample_postprocess(
                self.sample_dir,
                enable_vlm=self.postprocess_enable_vlm,
                vlm_every=self.postprocess_vlm_every,
                vlm_max_calls=self.postprocess_vlm_max_calls,
                ocr_every=self.postprocess_ocr_every,
                enable_flow_labeling=self.enable_flow_labeling,
                flow_app_type=self.app_config.app_type,
                flow_scene=self.scene_config.name,
            )
            flow_summary = self.postprocess_summary.get("flow_labeling") or {}
            if flow_summary.get("output_file"):
                self.flow_labeled_file = str(flow_summary["output_file"])
            status = self.postprocess_summary.get("status", "unknown")
            print(f"[后处理] 完成，状态: {status}")
            print(f"[后处理] 汇总: {self.sample_dir / 'postprocess' / 'summary.json'}")
        except Exception as exc:
            # Raw collection is already complete. Post-processing must not turn a
            # valid capture into a failed sample.
            print(f"[警告] 后处理失败，原始采集文件已保留: {type(exc).__name__}: {exc}")
    
    def _has_real_qoe_csv(self, qoe_path: Path) -> bool:
        """判断 QoE monitor 是否已经写入真实检测数据。"""
        if not qoe_path.exists() or qoe_path.stat().st_size <= 0:
            return False
        try:
            with open(qoe_path, 'r', encoding='utf-8', newline='') as f:
                reader = csv.reader(f)
                rows = list(reader)
        except Exception as e:
            print(f"[警告] 读取QoE文件失败，将生成占位QoE: {e}")
            return False

        if len(rows) <= 1:
            return False

        header = rows[0]
        required = {'file_name', 'time', 'rtt', 'trust_resolution', 'trust_stall'}
        if not required.issubset(set(header)):
            print(f"[警告] QoE文件表头不完整，将生成占位QoE: {header}")
            return False

        return True

    def _generate_placeholder_qoe_csv(self, qoe_path: Path):
        """生成占位 QoE 数据，仅用于实时 QoE 不可用的兜底场景。"""
        pcap_ext = '.pcapng' if (self.pcap_file and self.pcap_file.endswith('.pcapng')) else '.pcap'
        rows = generate_qoe_csv(
            prefix=self.prefix,
            start_time=self.start_time,
            end_time=self.end_time,
            warmup_seconds=10,
            pcap_ext=pcap_ext,
        )
        with open(qoe_path, 'w', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            writer.writerows(rows)

    def _generate_empty_qoe_csv(self, qoe_path: Path):
        """生成QoE标注不适用文件，仅保留时间戳，识别内容为空。"""
        pcap_ext = '.pcapng' if (self.pcap_file and self.pcap_file.endswith('.pcapng')) else '.pcap'
        rows = generate_empty_qoe_csv(
            prefix=self.prefix,
            start_time=self.start_time,
            end_time=self.end_time,
            pcap_ext=pcap_ext,
        )
        with open(qoe_path, 'w', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            writer.writerows(rows)

    def _generate_empty_sa(self, sa_path: Path):
        """生成空的SA表头"""
        with open(sa_path, 'w', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            writer.writerow(generate_sa_csv_header())
        self.sa_file = str(sa_path)
        print(f"[生成] SA文件: {sa_path}")
    
    def parse_pcap_to_sa(self, pcap_file: str) -> List[List]:
        """解析pcap文件生成SA数据"""
        if not SCAPY_AVAILABLE:
            print("[警告] scapy未安装，无法解析pcap")
            return []
        
        try:
            from collections import defaultdict
            from scapy.all import rdpcap, IP, TCP, UDP, DNS
            
            packets = rdpcap(pcap_file)
            
            # 提取DNS记录
            ip_to_domain = {}
            print("[SA解析] 提取DNS记录...")
            for pkt in packets:
                if pkt.haslayer(DNS) and pkt.haslayer(IP):
                    dns = pkt[DNS]
                    if dns.qr == 1 and dns.an:
                        for i in range(dns.ancount):
                            try:
                                ans = dns.an[i]
                                if ans.type == 1:
                                    domain = ans.rrname.decode().rstrip('.') if isinstance(ans.rrname, bytes) else str(ans.rrname).rstrip('.')
                                    ip = ans.rdata
                                    if isinstance(ip, bytes):
                                        ip = '.'.join(str(b) for b in ip)
                                    ip_to_domain[ip] = domain
                                    ip_to_domain[domain] = domain
                            except:
                                pass
            
            print(f"[SA解析] 发现 {len(ip_to_domain)//2} 个DNS映射")
            
            known_domains = {
                '180.101.': 'BaiDu_CDN',
                '220.181.': 'BaiDu_Service',
                '111.202.': 'Douyin_CDN',
                '112.': 'Ali_CDN',
                '116.': 'Tencent_CDN',
                '120.': 'NetEase_CDN',
                '14.215.': 'Baidu_CDN',
                '153.3.': 'CMCC_CDN',
                '183.2.': 'Telecom_CDN',
            }
            
            flows = defaultdict(lambda: {
                'packet_num': 0,
                'upper_packet_num': 0,
                'down_packet_num': 0,
                'bytes': 0,
                'upper_bytes': 0,
                'down_bytes': 0,
                'has_syn': 0,
                'start_time': None,
                'domains': set(),
                'dst_ips': set()
            })
            
            device_ips = set(self.device_info.get('ipv4', []) + self.device_info.get('ipv6', []))
            
            for pkt in packets:
                if not pkt.haslayer(IP):
                    continue
                
                ip_layer = pkt[IP]
                src_ip = ip_layer.src
                dst_ip = ip_layer.dst
                proto = ip_layer.proto
                pkt_len = len(pkt)
                pkt_time = float(pkt.time)
                
                src_port = 0
                dst_port = 0
                if pkt.haslayer(TCP):
                    src_port = pkt[TCP].sport
                    dst_port = pkt[TCP].dport
                    has_syn = 1 if pkt[TCP].flags.S else 0
                elif pkt.haslayer(UDP):
                    src_port = pkt[UDP].sport
                    dst_port = pkt[UDP].dport
                    has_syn = 0
                else:
                    continue
                
                # 流标识（方向规范化）
                if src_ip < dst_ip or (src_ip == dst_ip and src_port < dst_port):
                    flow_key = (src_ip, dst_ip, src_port, dst_port, proto)
                    is_upper = True
                else:
                    flow_key = (dst_ip, src_ip, dst_port, src_port, proto)
                    is_upper = False
                
                if device_ips:
                    if src_ip in device_ips:
                        is_upper = True
                    elif dst_ip in device_ips:
                        is_upper = False
                
                flow = flows[flow_key]
                flow['packet_num'] += 1
                flow['bytes'] += pkt_len
                
                if is_upper:
                    flow['upper_packet_num'] += 1
                    flow['upper_bytes'] += pkt_len
                else:
                    flow['down_packet_num'] += 1
                    flow['down_bytes'] += pkt_len
                
                if has_syn:
                    flow['has_syn'] = 1
                
                if flow['start_time'] is None or pkt_time < flow['start_time']:
                    flow['start_time'] = pkt_time
                
                flow['dst_ips'].add(dst_ip)
                
                if src_ip in ip_to_domain:
                    flow['domains'].add(ip_to_domain[src_ip])
                if dst_ip in ip_to_domain:
                    flow['domains'].add(ip_to_domain[dst_ip])
                
                if not flow['domains']:
                    for prefix, domain in known_domains.items():
                        if dst_ip.startswith(prefix):
                            flow['domains'].add(domain)
                            break
            
            sa_data = [[
                'uri', 'app_type', 'app_name', 'packet_num', 'upper_packet_num',
                'down_packet_num', 'syn_flag', 'flow_bytes', 'upper_bytes', 'down_bytes',
                'srcip', 'dstip', 'srcport', 'dstport', 'l4proto', 'domain',
                'flow_start_time', 'not_in_app', 'egn_sub_protocol'
            ]]
            
            for flow_key, flow in flows.items():
                src_ip, dst_ip, src_port, dst_port, proto = flow_key
                proto_name = '6' if proto == 6 else ('17' if proto == 17 else str(proto))
                
                if flow['start_time']:
                    start_time_str = datetime.fromtimestamp(flow['start_time']).strftime('%Y/%m/%d %H:%M')
                else:
                    start_time_str = datetime.now().strftime('%Y/%m/%d %H:%M')
                
                not_in_app = 0 if flow['packet_num'] > 2 else 1
                egn_proto = self._identify_protocol(dst_port, proto)
                
                domain = 'nan'
                if flow['domains']:
                    domain = list(flow['domains'])[0]
                else:
                    for prefix, dom in known_domains.items():
                        if dst_ip.startswith(prefix):
                            domain = dom
                            break
                
                pcap_name = (
                    os.path.basename(self.pcap_file)
                    if self.pcap_file
                    else build_pcap_filename(self.prefix)
                )
                
                sa_data.append([
                    pcap_name,
                    self.app_config.app_type,
                    self.app_config.name,
                    flow['packet_num'],
                    flow['upper_packet_num'],
                    flow['down_packet_num'],
                    flow['has_syn'],
                    flow['bytes'],
                    flow['upper_bytes'],
                    flow['down_bytes'],
                    src_ip,
                    dst_ip,
                    src_port,
                    dst_port,
                    proto_name,
                    domain,
                    start_time_str,
                    not_in_app,
                    egn_proto
                ])
            
            return sa_data
            
        except Exception as e:
            print(f"[错误] pcap解析失败: {e}")
            import traceback
            traceback.print_exc()
            return []
    
    def _identify_protocol(self, dst_port: int, proto: int) -> str:
        """根据端口识别应用层协议"""
        protocol_map = {
            (6, 80): 'HTTP',
            (6, 443): 'HTTPS',
            (6, 8080): 'HTTP_Proxy',
            (6, 22): 'SSH',
            (6, 53): 'DNS_TCP',
            (17, 53): 'DNS',
            (17, 123): 'NTP',
            (17, 443): 'QUIC',
            (17, 1935): 'RTMP',
            (6, 1935): 'RTMP_TCP',
            (17, 3478): 'STUN',
            (17, 5349): 'STUN_TLS',
        }
        
        key = (proto, dst_port)
        if key in protocol_map:
            return protocol_map[key]
        
        if self.app_config.app_type == 'video':
            if dst_port in [80, 443, 8080, 8443]:
                return 'HTTP_Video'
            elif dst_port in [1935, 4433, 8000, 8088]:
                return 'Video_Stream'
            elif proto == 17 and dst_port > 10000:
                return 'Video_UDP'
        elif self.app_config.app_type == 'game':
            if 1000 <= dst_port <= 20000:
                return 'Game_Protocol'
        elif self.app_config.app_type == 'social':
            if dst_port in [443, 5222, 5223, 5228]:
                return 'IM_Protocol'
        
        return 'Unknown'
    
    def run(
        self,
        external_control: bool = False,
        stop_file: Optional[str] = None,
        ready_file: Optional[str] = None,
    ) -> bool:
        """运行完整采集流程"""
        try:
            self.external_control = external_control or self.external_control
            # 检查环境
            if not self.check_env():
                return False
            
            # 准备阶段
            self.prepare()
            
            # 开始采集
            self.start_capture()
            
            if self.external_control:
                self.write_external_ready(ready_file)
                self.wait_for_external_stop(stop_file)
            else:
                # 执行场景
                self.execute_scene()
            
            # 停止采集
            self.stop_capture()
            
            # 收集文件
            self.collect_files()
            
            # 生成元数据
            self.generate_metadata()

            # 采集产物全部落盘后自动执行问题4/5/6后处理。
            self.run_postprocess()
            
            print("\n" + "="*60)
            print("采集完成!")
            print(f"文件前缀: {self.prefix}")
            print(f"样本目录: {self.sample_dir}")
            print(f"抓包模式: {'Root(tcpdump)' if self.use_root else '无Root(VpnService App)'}")
            if self.pcap_file:
                print(f"pcap文件: {self.pcap_file}")
            if self.video_file:
                print(f"录屏文件: {self.video_file}")
            print(f"yaml文件: {self.yaml_file}")
            print(f"qoe文件: {self.qoe_file}")
            print(f"sa文件: {self.sa_file}")
            if self.flow_labeled_file:
                print(f"流级标注文件: {self.flow_labeled_file}")
            print(f"log文件: {self.log_file}")
            print("="*60)
            return True
            
        except KeyboardInterrupt:
            print("\n[中断] 用户中断采集")
            self.stop_capture()
            self.cleanup_device_files()
            return False
        except Exception as e:
            print(f"\n[错误] 采集失败: {e}")
            self.stop_capture()
            self.cleanup_device_files()
            raise


def main():
    parser = argparse.ArgumentParser(
        description='安卓端侧APP样本采集工具 (支持Root/无Root双模式)',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  # 自动模式(优先root, 无root则使用PCAPdroid)
  python app_collector.py --app-name mango --scene movie --duration 600
  
  # 强制无root模式(需提前安装PCAPdroid)
  python app_collector.py --app-name mango --scene movie --capture-mode noroot
  
  # 强制root模式
  python app_collector.py --app-name mango --scene movie --capture-mode root
  
  # 指定自定义抓包App包名
  python app_collector.py --app-name mango --scene movie --capture-mode noroot --pcap-app-package com.xxx.xxx
  
  # 全量抓包（保留所有数据，文件大）
  python app_collector.py --app-name mango --scene movie --pcap-mode full
  
  # 清理设备上的临时文件
  python app_collector.py --cleanup --device abc123
        """
    )
    
    parser.add_argument('--app-name', '-a', help='应用名称(如: mango, honorofkings, wechat)')
    parser.add_argument('--scene', '-s', help='采集场景(如: movie, battle, video_call)')
    parser.add_argument('--device', '-d', help='设备ID(adb devices查看)')
    parser.add_argument('--duration', '-t', type=int, help='采集时长(秒)，默认使用场景配置')
    parser.add_argument('--target-business-duration', type=int,
                        help='业务开始后需要保留的有效时长（秒）')
    parser.add_argument('--resolution', '-r', help='视频分辨率(如: "流畅360P", "高清720P")')
    parser.add_argument('--output', '-o', default='./data', help='输出目录(默认: ./data)')
    parser.add_argument('--location', '-l', default='default', help='采集地点标识')
    parser.add_argument('--no-pcap', action='store_true', help='禁用pcap抓包（默认启用）')
    parser.add_argument('--pcap-mode', choices=['minimal', 'standard', 'full'], default='standard',
                       help='抓包模式: minimal(最小), standard(标准), full(完整)')
    parser.add_argument('--capture-mode', choices=['auto', 'root', 'noroot'], default='auto',
                       help='采集模式: auto(自动), root(tcpdump需root), noroot(VpnService App无root)')
    parser.add_argument('--pcap-app-package', help='指定PCAPdroid app_filter目标应用包名(如: com.ss.android.ugc.aweme)')
    parser.add_argument('--pcapdroid-api-key', help='PCAPdroid Control Permissions中生成的API Key（跳过授权对话框）')
    parser.add_argument('--custom-app-type', default=None,
                       help='自定义应用类型，如 live/movie/game/social')
    parser.add_argument('--custom-app-scene', default=None,
                       help='自定义应用场景')
    parser.add_argument('--enable-qoe', action='store_true', default=True,
                       help='启用QoE实时监测(默认开启)')
    parser.add_argument('--disable-qoe', action='store_true',
                        help='禁用QoE实时监测')
    parser.add_argument('--disable-postprocess', action='store_true',
                       help='禁用采集结束后的全部后处理（默认开启）')
    parser.add_argument('--disable-flow-labeling', action='store_true',
                       help='禁用采集结束后的流级标注（默认开启）')
    parser.add_argument('--postprocess-disable-vlm', action='store_true',
                        help='后处理不调用视觉模型，仅使用logcat/OCR/录屏时序规则')
    parser.add_argument('--postprocess-vlm-every', type=int, default=5,
                        help='业务开始粗定位阶段每隔多少秒调用一次VLM（默认5）')
    parser.add_argument('--postprocess-vlm-max-calls', type=int, default=20,
                        help='每个样本最多调用VLM的次数（默认20）')
    parser.add_argument('--postprocess-ocr-every', type=int, default=1,
                        help='无延迟显示时后处理OCR采样间隔秒数（默认1）')
    parser.add_argument('--external-control', action='store_true',
                       help='外部控制模式：只运行录屏/logcat/抓包/QoE采集，不启动/停止目标App，不执行场景等待')
    parser.add_argument('--clear-app', action='store_true',
                       help='采集前清理应用数据（会清除登录信息，默认不清理）')
    parser.add_argument('--stop-file',
                       help='外部控制模式下的停止信号文件路径；文件出现后停止采集并收集产物')
    parser.add_argument('--ready-file',
                       help='外部控制模式下的就绪信号文件路径；采集初始化完成后创建')
    parser.add_argument('--list-apps', action='store_true',
                        help='列出所有支持的APP及其场景名')
    parser.add_argument('--list-scenes', action='store_true',
                        help='列出 --app-name 指定APP的场景详情')
    parser.add_argument('--cleanup', action='store_true', help='清理设备上的临时文件')
    
    args = parser.parse_args()
    
    # 清理设备上的临时文件
    if args.cleanup:
        print("[清理] 清理设备临时文件...")
        adb = ADBHelper(args.device)
        if not adb.check_connection():
            print("[错误] 设备未连接")
            return 1
        
        temp_files = [
            '/data/local/tmp/screenrecord.mp4',
            '/data/local/tmp/capture.pcap',
            '/sdcard/screen.mp4',
            '/sdcard/capture.pcap',
            '/sdcard/Download/pcap_collector/screenrecord.mp4',
            '/sdcard/Download/pcap_collector/capture.pcap',
        ]
        
        use_root = adb.root_available
        for file_path in temp_files:
            if use_root and file_path.startswith('/data/local/tmp'):
                adb.run_shell(f'rm -f {file_path}', use_root=True)
            else:
                adb.run(['shell', 'rm', '-f', file_path])
            print(f"[清理] 已清理: {file_path}")
        
        print("\n[清理] 临时文件清理完成")
        return 0
    
    # 列出所有APP
    if args.list_apps:
        from app_configs import list_all_apps
        print("支持的APP列表:")
        for app_name in list_all_apps():
            app = get_app_config(app_name)
            scene_names = ', '.join(scene.name for scene in app.scenes) if app else ''
            print(f"  - {app_name}: {scene_names}")
        print("\n使用 --app-name APP --list-scenes 查看场景说明和默认时长")
        return 0

    # 查询场景时只要求APP；不能反过来要求用户先知道scene。
    if args.list_scenes:
        if not args.app_name:
            print("错误: --list-scenes 必须同时指定 --app-name")
            print("示例: python app_collector.py --app-name douyin --list-scenes")
            return 1
        app_config = get_app_config(args.app_name)
        if not app_config:
            print(f"错误: 未找到应用 '{args.app_name}'")
            print("使用 --list-apps 查看所有支持的APP")
            return 1
        print(f"\n'{args.app_name}' 支持的采集场景:")
        for scene in app_config.scenes:
            print(f"  - {scene.name}: {scene.description} (默认{scene.duration}秒)")
        return 0
    
    # 检查必需的参数
    if not args.app_name or not args.scene:
        print("错误: 必须指定 --app-name 和 --scene 参数")
        print("使用 --help 查看帮助")
        return 1
    
    # 获取APP配置
    app_config = get_app_config(args.app_name)
    if not app_config:
        fallback_package = args.pcap_app_package
        if not fallback_package:
            print(f"错误: 未找到应用 '{args.app_name}'")
            print("使用 --list-apps 查看所有支持的APP，或提供 --pcap-app-package")
            return 1
        from app_configs import create_fallback_config
        app_config = create_fallback_config(
            args.app_name,
            fallback_package,
            custom_type=args.custom_app_type,
            # The collector must validate the same scene it was asked to run.
            custom_scene=args.scene,
        )
        print(f"[提示] 应用 '{args.app_name}' 使用自定义配置")
    
    # 检查场景
    if not app_config.get_scene(args.scene):
        print(f"错误: 场景 '{args.scene}' 不存在于应用 '{args.app_name}'")
        print(f"使用 --list-scenes 查看所有支持的场景")
        return 1
    
    # 创建采集器并运行
    collector = AppCollector(
        app_config=app_config,
        scene=args.scene,
        device_id=args.device,
        duration=args.duration,
        target_business_duration=args.target_business_duration,
        resolution=args.resolution,
        output_dir=args.output,
        location=args.location,
        pcap_mode=args.pcap_mode,
        capture_mode=args.capture_mode,
        pcap_app_package=args.pcap_app_package,
        pcapdroid_api_key=args.pcapdroid_api_key,
        enable_qoe=args.enable_qoe and not args.disable_qoe,
        external_control=args.external_control,
        clear_app=args.clear_app,
        enable_postprocess=not args.disable_postprocess,
        enable_flow_labeling=not args.disable_flow_labeling,
        postprocess_enable_vlm=not args.postprocess_disable_vlm,
        postprocess_vlm_every=args.postprocess_vlm_every,
        postprocess_vlm_max_calls=args.postprocess_vlm_max_calls,
        postprocess_ocr_every=args.postprocess_ocr_every,
    )
    
    # 设置是否抓包
    collector.enable_pcap = not args.no_pcap
    
    success = collector.run(
        external_control=args.external_control,
        stop_file=args.stop_file,
        ready_file=args.ready_file,
    )
    return 0 if success else 1


if __name__ == '__main__':
    sys.exit(main())
