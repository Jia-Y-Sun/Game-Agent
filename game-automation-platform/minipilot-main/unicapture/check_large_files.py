#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
手机大文件检查工具
扫描手机存储中超过指定大小的文件，帮助定位占用空间的文件

使用方法:
    python check_large_files.py                    # 默认检查大于50MB的文件
    python check_large_files.py -s 100             # 检查大于100MB的文件
    python check_large_files.py --cleanup          # 检查后删除确认的文件
    python check_large_files.py -p /sdcard/DCIM    # 只检查指定路径
"""

import sys
import argparse
import subprocess
from typing import List, Tuple, Optional
from datetime import datetime


class ADBHelper:
    """ADB工具类"""
    
    def __init__(self, device_id: Optional[str] = None):
        self.device_id = device_id
        self.base_cmd = self._build_base_cmd(device_id)
        self.connection_error = ''

    @staticmethod
    def _build_base_cmd(device_id: Optional[str]) -> list:
        return ['adb', '-s', device_id] if device_id else ['adb']

    def _select_device(self, device_id: str) -> None:
        self.device_id = device_id
        self.base_cmd = self._build_base_cmd(device_id)
    
    def run(self, cmd: list, capture_output: bool = True, timeout: int = 60) -> tuple:
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
    
    def check_connection(self) -> bool:
        """Resolve one ready device and pin later commands to its serial."""
        self.connection_error = ''
        try:
            result = subprocess.run(
                ['adb', 'devices'],
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
        ready_devices = [
            parts[0]
            for line in result.stdout.splitlines()[1:]
            if len(parts := line.split()) >= 2 and parts[1] == 'device'
        ]
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
    
    def has_root(self) -> bool:
        """检查是否有root权限"""
        success, stdout, _ = self.run(['shell', 'su', '-c', 'id'])
        return success and 'uid=0' in stdout


class LargeFileChecker:
    """大文件检查器"""
    
    # 默认扫描路径（按优先级排序）
    DEFAULT_PATHS = [
        # 临时文件目录
        '/data/local/tmp/',
        # 内置存储
        '/sdcard/',
        '/storage/emulated/0/',
        # 常见应用缓存目录
        '/sdcard/Android/data/',
        '/sdcard/Android/obb/',
        # 媒体目录
        '/sdcard/DCIM/',
        '/sdcard/Pictures/',
        '/sdcard/Movies/',
        '/sdcard/Download/',
        '/sdcard/Documents/',
        # 应用特定目录
        '/sdcard/tencent/',
        '/sdcard/WeChat/',
        '/sdcard/Android/media/',
    ]
    
    # 系统缓存目录（需要root）
    ROOT_PATHS = [
        '/data/system/',
        '/data/app/',
        '/data/dalvik-cache/',
        '/data/misc/',
        '/data/user/0/',
        '/cache/',
    ]
    
    def __init__(self, device_id: Optional[str] = None, size_threshold_mb: int = 50):
        self.adb = ADBHelper(device_id)
        self.size_threshold = size_threshold_mb * 1024 * 1024  # 转换为字节
        self.large_files: List[Tuple[str, int, str]] = []  # (路径, 大小, 修改时间)
        self.has_root = False
        
    def format_size(self, size_bytes: int) -> str:
        """格式化文件大小"""
        for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
            if size_bytes < 1024.0:
                return f"{size_bytes:.2f} {unit}"
            size_bytes /= 1024.0
        return f"{size_bytes:.2f} PB"
    
    def format_time(self, timestamp: int) -> str:
        """格式化时间戳"""
        try:
            dt = datetime.fromtimestamp(timestamp)
            return dt.strftime('%Y-%m-%d %H:%M:%S')
        except:
            return "Unknown"
    
    def check_path_exists(self, path: str) -> bool:
        """检查路径是否存在"""
        success, _, _ = self.adb.run(['shell', 'test', '-d', path])
        return success
    
    def find_large_files_in_path(self, path: str, use_root: bool = False) -> List[Tuple[str, int, str]]:
        """
        在指定路径查找大文件
        返回: [(文件路径, 文件大小, 修改时间), ...]
        """
        results = []
        
        # 使用find命令查找大于阈值的文件
        # 排除一些系统目录避免错误
        exclude_dirs = ['proc', 'sys', 'dev', 'acct', 'vendor', 'system']
        exclude_expr = ' '.join([f'-not -path "*/{d}/*"' for d in exclude_dirs])
        
        if use_root and self.has_root:
            # 使用root权限查找
            find_cmd = f'find {path} -type f -size +{self.size_threshold}c {exclude_expr} 2>/dev/null'
            ls_cmd = ' -exec ls -la {} + 2>/dev/null'
            cmd = ['shell', 'su', '-c', find_cmd + ls_cmd]
        else:
            # 普通权限查找
            find_cmd = f'find {path} -type f -size +{self.size_threshold}c {exclude_expr} 2>/dev/null'
            ls_cmd = ' -exec ls -la {} + 2>/dev/null'
            cmd = ['shell', find_cmd + ls_cmd]
        
        success, stdout, stderr = self.adb.run(cmd, timeout=120)
        
        if not success or not stdout:
            return results
        
        # 解析ls输出
        for line in stdout.strip().split('\n'):
            line = line.strip()
            if not line or line.startswith('total'):
                continue
            
            try:
                parts = line.split()
                if len(parts) >= 9:
                    # ls -la 格式: -rw-r--r-- 1 user group size month day time/year filename
                    size = int(parts[4])
                    # 文件名可能包含空格，需要从第8个字段开始拼接
                    filename = ' '.join(parts[8:])
                    # 尝试获取修改时间
                    time_str = ' '.join(parts[5:8])
                    
                    if size >= self.size_threshold:
                        results.append((filename, size, time_str))
            except (ValueError, IndexError):
                continue
        
        return results
    
    def quick_scan_common_paths(self) -> List[Tuple[str, int, str]]:
        """快速扫描常见的大文件路径"""
        results = []
        scanned_paths = set()
        
        print(f"\n[扫描] 开始扫描大于 {self.format_size(self.size_threshold)} 的文件...")
        print("-" * 80)
        
        # 优先检查已知的临时文件
        temp_files = [
            '/data/local/tmp/screenrecord.mp4',
            '/data/local/tmp/capture.pcap',
            '/sdcard/screen.mp4',
            '/sdcard/capture.pcap',
        ]
        
        print("\n[检查] 已知临时文件...")
        for file_path in temp_files:
            success, stdout, _ = self.adb.run(['shell', 'su', '-c', f'ls -la {file_path} 2>/dev/null'])
            if success and stdout:
                try:
                    parts = stdout.strip().split()
                    if len(parts) >= 5:
                        size = int(parts[4])
                        if size >= self.size_threshold:
                            time_str = ' '.join(parts[5:8])
                            results.append((file_path, size, time_str))
                            print(f"  [发现] {file_path} - {self.format_size(size)}")
                except:
                    pass
        
        # 扫描常见目录
        for path in self.DEFAULT_PATHS:
            if path in scanned_paths:
                continue
            scanned_paths.add(path)
            
            if not self.check_path_exists(path):
                continue
            
            print(f"\n[扫描] {path}")
            files = self.find_large_files_in_path(path, use_root=False)
            if files:
                for file_path, size, time_str in files:
                    results.append((file_path, size, time_str))
                    print(f"  [发现] {self.format_size(size):>12}  {file_path}")
            else:
                print(f"  [无大文件]")
        
        return results
    
    def deep_scan(self, path: Optional[str] = None) -> List[Tuple[str, int, str]]:
        """深度扫描（较慢但更全面）"""
        results = []
        
        target_paths = [path] if path else ['/sdcard/']
        
        print(f"\n[深度扫描] 扫描大于 {self.format_size(self.size_threshold)} 的文件...")
        print("-" * 80)
        
        for target_path in target_paths:
            if not self.check_path_exists(target_path):
                print(f"[跳过] 路径不存在: {target_path}")
                continue
            
            print(f"\n[扫描] {target_path} (可能需要较长时间...)")
            files = self.find_large_files_in_path(target_path, use_root=self.has_root)
            
            for file_path, size, time_str in sorted(files, key=lambda x: x[1], reverse=True):
                results.append((file_path, size, time_str))
                print(f"  [发现] {self.format_size(size):>12}  {file_path}")
        
        return results
    
    def scan_app_cache(self) -> List[Tuple[str, int, str]]:
        """扫描应用缓存目录（需要root）"""
        if not self.has_root:
            print("\n[提示] 需要root权限才能扫描应用缓存目录")
            return []
        
        results = []
        print("\n[扫描] 应用缓存目录（需要root）...")
        print("-" * 80)
        
        # 扫描 /data/data/ 下各个应用的缓存
        cache_paths = [
            '/data/data/*/cache/',
            '/data/data/*/files/',
            '/data/user/0/*/cache/',
            '/data/user/0/*/files/',
        ]
        
        for cache_pattern in cache_paths:
            cmd = ['shell', 'su', '-c', 
                   f'find {cache_pattern} -type f -size +{self.size_threshold}c 2>/dev/null '
                   '-exec ls -la {} + 2>/dev/null | head -20']
            success, stdout, _ = self.adb.run(cmd, timeout=60)
            
            if success and stdout:
                for line in stdout.strip().split('\n'):
                    try:
                        parts = line.split()
                        if len(parts) >= 9:
                            size = int(parts[4])
                            filename = ' '.join(parts[8:])
                            time_str = ' '.join(parts[5:8])
                            results.append((filename, size, time_str))
                            print(f"  [发现] {self.format_size(size):>12}  {filename}")
                    except:
                        pass
        
        return results
    
    def delete_file(self, file_path: str, use_root: bool = False) -> bool:
        """删除指定文件"""
        if use_root or self.has_root:
            success, _, stderr = self.adb.run(['shell', 'su', '-c', f'rm -f "{file_path}"'])
        else:
            success, _, stderr = self.adb.run(['shell', f'rm -f "{file_path}"'])
        
        if success:
            print(f"  [已删除] {file_path}")
            return True
        else:
            print(f"  [删除失败] {file_path}: {stderr}")
            return False
    
    def interactive_cleanup(self, files: List[Tuple[str, int, str]]):
        """交互式清理"""
        if not files:
            print("\n[提示] 没有发现大文件，无需清理")
            return
        
        print("\n" + "=" * 80)
        print("发现的大文件列表（按大小排序）:")
        print("=" * 80)
        
        # 按大小排序
        sorted_files = sorted(files, key=lambda x: x[1], reverse=True)
        
        for idx, (file_path, size, time_str) in enumerate(sorted_files, 1):
            print(f"\n{idx}. {self.format_size(size):>12}  {file_path}")
            print(f"   修改时间: {time_str}")
        
        print("\n" + "=" * 80)
        print("清理选项:")
        print("  输入数字 (1-N)  - 删除对应文件")
        print("  all             - 删除所有列出的文件")
        print("  temp            - 删除已知临时文件")
        print("  q/enter         - 退出不删除")
        print("=" * 80)
        
        choice = input("\n请选择: ").strip().lower()
        
        if choice in ['q', 'quit', '']:
            print("[取消] 未删除任何文件")
            return
        
        if choice == 'all':
            print("\n[删除] 正在删除所有文件...")
            for file_path, size, _ in sorted_files:
                self.delete_file(file_path, use_root=True)
        
        elif choice == 'temp':
            print("\n[删除] 正在删除已知临时文件...")
            temp_patterns = ['screenrecord', 'capture.pcap', '.tmp', '.cache']
            deleted = 0
            for file_path, size, _ in sorted_files:
                if any(pattern in file_path for pattern in temp_patterns):
                    if self.delete_file(file_path, use_root=True):
                        deleted += 1
            print(f"\n[完成] 删除了 {deleted} 个临时文件")
        
        else:
            try:
                idx = int(choice) - 1
                if 0 <= idx < len(sorted_files):
                    file_path, size, _ = sorted_files[idx]
                    confirm = input(f"确认删除 {file_path} ({self.format_size(size)})? (y/n): ").strip().lower()
                    if confirm == 'y':
                        self.delete_file(file_path, use_root=True)
                    else:
                        print("[取消] 未删除文件")
                else:
                    print("[错误] 无效的序号")
            except ValueError:
                print("[错误] 无效的输入")
    
    def print_summary(self, files: List[Tuple[str, int, str]]):
        """打印汇总信息"""
        if not files:
            print("\n[汇总] 未发现大于阈值的大文件")
            return
        
        total_size = sum(f[1] for f in files)
        print("\n" + "=" * 80)
        print("扫描结果汇总:")
        print("=" * 80)
        print(f"发现文件数量: {len(files)}")
        print(f"总占用空间: {self.format_size(total_size)}")
        print(f"平均文件大小: {self.format_size(total_size / len(files))}")
        print(f"最大文件: {max(files, key=lambda x: x[1])[0]}")
        print(f"最大文件大小: {self.format_size(max(f[1] for f in files))}")
        print("=" * 80)


def main():
    parser = argparse.ArgumentParser(
        description='手机大文件检查工具 - 扫描并清理手机存储中的大文件',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  # 快速扫描默认路径，检查大于50MB的文件
  python check_large_files.py
  
  # 检查大于100MB的文件
  python check_large_files.py -s 100
  
  # 深度扫描整个sdcard
  python check_large_files.py --deep
  
  # 扫描指定路径
  python check_large_files.py -p /sdcard/DCIM/ -s 10
  
  # 交互式清理模式
  python check_large_files.py --cleanup
  
  # 扫描应用缓存（需要root）
  python check_large_files.py --cache
        """
    )
    
    parser.add_argument('-s', '--size', type=int, default=50,
                       help='文件大小阈值(MB)，默认50MB')
    parser.add_argument('-d', '--device', help='设备ID(adb devices查看)')
    parser.add_argument('-p', '--path', help='指定扫描路径')
    parser.add_argument('--deep', action='store_true', 
                       help='深度扫描（较慢但更全面）')
    parser.add_argument('--cache', action='store_true',
                       help='扫描应用缓存目录（需要root）')
    parser.add_argument('--cleanup', action='store_true',
                       help='交互式清理模式')
    parser.add_argument('--auto-clean-temp', action='store_true',
                       help='自动删除已知的临时文件（screenrecord, pcap等）')
    
    args = parser.parse_args()
    
    # 创建检查器
    checker = LargeFileChecker(device_id=args.device, size_threshold_mb=args.size)
    
    # 检查设备连接
    print("[检查] 检查设备连接...")
    if not checker.adb.check_connection():
        print(f"[错误] {checker.adb.connection_error or '未检测到已连接的设备'}")
        print("  1. 手机是否通过USB连接")
        print("  2. USB调试是否已开启")
        print("  3. 是否已授权调试")
        return 1
    print("[检查] 设备已连接")
    
    # 检查root权限
    checker.has_root = checker.adb.has_root()
    if checker.has_root:
        print("[检查] 设备已root")
    else:
        print("[检查] 设备未root，部分目录无法扫描")
    
    all_files = []
    
    # 根据参数执行不同扫描
    if args.cache:
        # 只扫描应用缓存
        files = checker.scan_app_cache()
        all_files.extend(files)
    
    elif args.deep:
        # 深度扫描
        files = checker.deep_scan(args.path)
        all_files.extend(files)
    
    elif args.path:
        # 扫描指定路径
        print(f"\n[扫描] 扫描指定路径: {args.path}")
        files = checker.find_large_files_in_path(args.path, use_root=checker.has_root)
        for file_path, size, time_str in sorted(files, key=lambda x: x[1], reverse=True):
            all_files.append((file_path, size, time_str))
            print(f"  [发现] {checker.format_size(size):>12}  {file_path}")
    
    else:
        # 快速扫描默认路径
        files = checker.quick_scan_common_paths()
        all_files.extend(files)
    
    # 去重
    seen = set()
    unique_files = []
    for f in all_files:
        if f[0] not in seen:
            seen.add(f[0])
            unique_files.append(f)
    all_files = unique_files
    
    # 打印汇总
    checker.print_summary(all_files)
    
    # 自动清理临时文件
    if args.auto_clean_temp:
        print("\n[自动清理] 正在删除已知临时文件...")
        temp_patterns = ['screenrecord', 'capture.pcap', '.tmp', 'temp_', '.cache']
        deleted = 0
        for file_path, size, _ in all_files:
            if any(pattern in file_path for pattern in temp_patterns):
                if checker.delete_file(file_path, use_root=True):
                    deleted += 1
        print(f"\n[完成] 自动删除了 {deleted} 个临时文件")
    
    # 交互式清理
    elif args.cleanup:
        checker.interactive_cleanup(all_files)
    
    # 显示存储空间信息
    print("\n[存储空间] 手机存储使用情况:")
    success, stdout, _ = checker.adb.run(['shell', 'df', '-h'])
    if success and stdout:
        for line in stdout.strip().split('\n'):
            if '/data' in line or '/sdcard' in line or 'storage' in line:
                print(f"  {line}")
    
    return 0


if __name__ == '__main__':
    sys.exit(main())
