"""
全功能日志系统
功能：
- 多级别日志，支持控制台彩色输出（Windows 兼容）
- 文件输出 KV 格式，支持 JSON 格式可选
- 异步/同步写入
- 日志轮转（按日期、按大小）与自动清理
- 警告日志独立存储
- 多进程安全（使用 portalocker）
- 线程局部上下文绑定
- 全局未捕获异常钩子
- 配置热重载（优雅切换）
- 独立控制台窗口（tail -f 模式）
"""

import os
import sys
import threading
import traceback
import re
import queue
import time
import subprocess
import atexit
import copy
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Union, Optional, Dict, Callable, List
from enum import Enum

# 尝试导入可选的依赖
try:
    import portalocker
    PORTALOCKER_AVAILABLE = True
except ImportError:
    PORTALOCKER_AVAILABLE = False
    print("警告: portalocker 未安装，多进程日志写入可能不安全。建议安装: pip install portalocker", file=sys.stderr)

try:
    import colorama
    colorama.just_fix_windows_console()
    COLORAMA_AVAILABLE = True
except ImportError:
    COLORAMA_AVAILABLE = False

try:
    from .config_manager import config
    CONFIG_AVAILABLE = True
except ImportError:
    CONFIG_AVAILABLE = False


class LogLevel(Enum):
    DEBUG = 10
    INFO = 20
    WARNING = 30
    ERROR = 40
    CRITICAL = 50

    @classmethod
    def from_string(cls, level_str: str) -> 'LogLevel':
        levels = {
            'DEBUG': cls.DEBUG,
            'INFO': cls.INFO,
            'WARNING': cls.WARNING,
            'ERROR': cls.ERROR,
            'CRITICAL': cls.CRITICAL
        }
        return levels.get(level_str.upper(), cls.INFO)


class ConsoleColor:
    RESET = '\033[0m'
    BOLD = '\033[1m'
    DEBUG = '\033[90m'
    INFO = '\033[92m'
    WARNING = '\033[93m'
    ERROR = '\033[91m'
    CRITICAL = '\033[95m'

    @classmethod
    def get_color(cls, level: LogLevel) -> str:
        if level == LogLevel.DEBUG:
            return cls.DEBUG
        elif level == LogLevel.INFO:
            return cls.INFO
        elif level == LogLevel.WARNING:
            return cls.WARNING
        elif level == LogLevel.ERROR:
            return cls.ERROR
        elif level == LogLevel.CRITICAL:
            return cls.CRITICAL + cls.BOLD
        return cls.RESET


def escape_kv_value(value: Any) -> str:
    """转义 KV 值中的特殊字符"""
    if value is None:
        return ''
    if not isinstance(value, str):
        value = str(value)
    value = value.replace('\\', '\\\\')
    value = value.replace('\n', '\\n')
    value = value.replace('\r', '\\r')
    value = value.replace('\t', '\\t')
    value = value.replace('=', '\\=')
    value = value.replace(' ', '\\s')
    value = value.replace(',', '\\,')
    value = value.replace('"', '\\"')
    return value


def unescape_kv_value(value: str) -> str:
    """反转义"""
    if not value:
        return value
    value = value.replace('\\n', '\n')
    value = value.replace('\\r', '\r')
    value = value.replace('\\t', '\t')
    value = value.replace('\\=', '=')
    value = value.replace('\\s', ' ')
    value = value.replace('\\,', ',')
    value = value.replace('\\"', '"')
    value = value.replace('\\\\', '\\')
    return value


class AsyncLogWriter:
    """异步日志写入器（支持优雅关闭，避免死锁）"""
    def __init__(self, write_func, maxsize=5000):
        self.write_func = write_func   # 同步写入函数
        self.queue = queue.Queue(maxsize=maxsize)
        self._stop = threading.Event()
        self._worker_thread = threading.Thread(target=self._worker, daemon=True)
        self._worker_thread.start()

    def _worker(self):
        while not self._stop.is_set():
            try:
                content = self.queue.get(timeout=0.5)
                if content is None:  # 哨兵，表示退出
                    break
                self.write_func(content)
                self.queue.task_done()
            except queue.Empty:
                continue
            except Exception as e:
                sys.stderr.write(f"异步日志写入失败: {e}\n")

    def write(self, content: str):
        try:
            self.queue.put(content, block=False)
        except queue.Full:
            # 队列满时丢弃最旧的一条并尝试再次放入
            try:
                self.queue.get_nowait()
                self.queue.task_done()
                self.queue.put(content)
                # 可选：记录丢弃日志（避免频繁输出）
                # sys.stderr.write("警告: 异步日志队列已满，丢弃最旧日志\n")
            except queue.Empty:
                pass

    def flush(self):
        """
        刷新缓冲区（不阻塞等待，避免死锁）
        实际写入由后台线程负责，此方法仅占位，保证接口一致。
        """
        pass

    def close(self, timeout=5):
        """
        优雅关闭，等待队列清空后退出。
        若超时，则强制清空队列并标记完成，防止残留任务影响后续测试。
        """
        self._stop.set()
        self.queue.put(None)  # 发送退出信号
        self._worker_thread.join(timeout=timeout)
        if self._worker_thread.is_alive():
            sys.stderr.write("警告: 异步日志写入器未能在超时内完全关闭，可能丢失部分日志\n")
        # 清理残留队列任务，避免下次 join 时永久等待
        while not self.queue.empty():
            try:
                self.queue.get_nowait()
                self.queue.task_done()
            except queue.Empty:
                break


class WarningLogManager:
    """警告/错误日志独立存储管理器"""
    def __init__(self, log_dir: Path, base_name: str = "log"):
        if not isinstance(log_dir, Path):
            log_dir = Path(log_dir)
        self.log_dir = log_dir
        self.base_name = base_name
        self.current_file = None
        self.current_date = None
        self.current_sequence = 1
        self.lock = threading.RLock()
        self.warning_dir = self.log_dir / "warnings"
        self.warning_dir.mkdir(parents=True, exist_ok=True)

    def get_filename(self, date: datetime, sequence: int) -> str:
        return f"warning_{date.strftime('%Y%m%d')}-{sequence:03d}.log"

    def get_current_filename(self) -> str:
        now = datetime.now()
        today = now.date()
        if self.current_date != today:
            self.current_date = today
            self.current_sequence = 1
        return str(self.warning_dir / self.get_filename(now, self.current_sequence))

    def write(self, content: str):
        with self.lock:
            try:
                filename = self.get_current_filename()
                if not self.current_file or self.current_file.name != filename:
                    if self.current_file:
                        self.current_file.close()
                    self.current_file = open(filename, 'a', encoding='utf-8')
                self.current_file.write(content)
                self.current_file.flush()
            except Exception as e:
                sys.stderr.write(f"警告日志写入失败: {e}\n")

    def close(self):
        with self.lock:
            if self.current_file:
                self.current_file.close()
                self.current_file = None


class LogFileManager:
    """
    日志文件管理器 - 支持同步/异步写入、轮转、清理、多进程安全
    """
    def __init__(self,
                 log_dir: str,
                 base_name: str = "log",
                 async_enabled: bool = False,
                 async_queue_size: int = 5000,
                 max_size_mb: int = 100,
                 retention_days: int = 30,
                 max_total_size_mb: int = 1024,
                 warning_separate: bool = True,
                 rotation_by_date: bool = True,
                 rotation_by_size: bool = True):
        self.log_dir = Path(log_dir)
        self.base_name = base_name
        self.current_file = None
        self.current_date = None
        self.current_sequence = 1
        self.lock = threading.RLock()
        self.max_size_mb = max_size_mb
        self.retention_days = retention_days
        self.max_total_size_mb = max_total_size_mb
        self.warning_separate = warning_separate
        self.rotation_by_date = rotation_by_date
        self.rotation_by_size = rotation_by_size

        self.log_dir.mkdir(parents=True, exist_ok=True)

        self.warning_manager = WarningLogManager(self.log_dir, base_name) if warning_separate else None

        self.async_enabled = async_enabled
        if async_enabled:
            self.writer = AsyncLogWriter(self._real_write, maxsize=async_queue_size)
        else:
            self.writer = None

        # 执行一次清理
        self.cleanup()

    def _real_write(self, content: str):
        """实际写入文件（同步，由异步 writer 或直接调用）"""
        with self.lock:
            try:
                filename = self._get_current_filename()
                if not self.current_file or self.current_file.name != filename:
                    if self.current_file:
                        self.current_file.close()
                    self.current_file = open(filename, 'a', encoding='utf-8')
                self.current_file.write(content)
                self.current_file.flush()

                if self.warning_manager and (
                    'level=WARNING' in content or
                    'level=ERROR' in content or
                    'level=CRITICAL' in content
                ):
                    self.warning_manager.write(content)
            except Exception as e:
                sys.stderr.write(f"日志写入失败: {e}\n")

    def write(self, content: str):
        """对外写入接口（同步或异步）"""
        if self.async_enabled:
            self.writer.write(content)
        else:
            self._real_write(content)

    def _get_current_filename(self) -> str:
        """
        获取当前应写入的文件名，支持多进程安全：扫描目录获取最大序号
        """
        now = datetime.now()
        today = now.date()
        if self.rotation_by_date and self.current_date != today:
            self.current_date = today
            self.current_sequence = self._get_next_sequence(now.strftime('%Y%m%d'))
        else:
            if self.rotation_by_size and self.current_file:
                try:
                    current_size_mb = self.current_file.tell() / (1024 * 1024)
                    if current_size_mb >= self.max_size_mb:
                        # 需要轮转，增加序号并重新扫描确保唯一
                        new_seq = self._get_next_sequence(now.strftime('%Y%m%d'))
                        # 如果新序号大于当前序号，更新；否则当前序号+1并再次检查
                        if new_seq > self.current_sequence:
                            self.current_sequence = new_seq
                        else:
                            self.current_sequence += 1
                except:
                    pass
        return str(self.log_dir / f"{now.strftime('%Y%m%d')}-{self.current_sequence:03d}.log")

    def _get_next_sequence(self, date_str: str) -> int:
        """
        跨进程安全地获取下一个可用的日志序号
        扫描目录中已存在的同一日期文件，取最大序号+1
        如果无法使用 portalocker，则降级为仅扫描（存在极小概率冲突）
        """
        # 扫描现有文件
        pattern = f"{date_str}-*.log"
        existing = list(self.log_dir.glob(pattern))
        seqs = []
        for p in existing:
            stem = p.stem
            if '-' in stem:
                try:
                    seq = int(stem.split('-')[-1])
                    seqs.append(seq)
                except ValueError:
                    pass
        max_seq = max(seqs) if seqs else 0
        candidate = max_seq + 1

        # 如果有 portalocker，可以尝试创建文件锁来确保唯一性（此处简化，仅靠扫描+重试）
        # 为了更高安全性，可在此处使用文件锁创建一个 .lock 文件
        if PORTALOCKER_AVAILABLE:
            lock_file = self.log_dir / f".lock_{date_str}"
            try:
                with open(lock_file, 'a') as lf:
                    portalocker.lock(lf, portalocker.LOCK_EX)
                    # 双重检查：可能在获得锁期间其他进程已创建文件
                    existing = list(self.log_dir.glob(pattern))
                    seqs = [int(p.stem.split('-')[-1]) for p in existing if p.stem.split('-')[-1].isdigit()]
                    candidate = max(seqs, default=0) + 1
                    # 解锁会在退出时自动释放
            except Exception:
                pass
        return candidate

    def flush(self):
        """刷新缓冲区"""
        if self.async_enabled:
            self.writer.flush()
        elif self.current_file:
            self.current_file.flush()

    def cleanup(self):
        """清理过期日志文件（按天/总大小）"""
        if not self.log_dir.exists():
            return
        now = datetime.now()
        # 按保留天数清理
        if self.retention_days > 0:
            cutoff = now - timedelta(days=self.retention_days)
            for f in self.log_dir.glob("*.log"):
                m = re.search(r'(\d{8})', f.name)
                if m:
                    try:
                        f_date = datetime.strptime(m.group(1), '%Y%m%d')
                        if f_date < cutoff:
                            f.unlink()
                    except:
                        pass
        # 按总大小清理
        if self.max_total_size_mb > 0:
            all_logs = list(self.log_dir.glob("*.log"))
            total = sum(f.stat().st_size for f in all_logs)
            max_bytes = self.max_total_size_mb * 1024 * 1024
            if total > max_bytes:
                # 按修改时间排序，删除最旧的
                files_sorted = sorted(all_logs, key=lambda f: f.stat().st_mtime)
                for f in files_sorted:
                    if total <= max_bytes:
                        break
                    total -= f.stat().st_size
                    f.unlink()

    def close(self):
        """关闭文件管理器"""
        self.flush()
        if self.async_enabled:
            self.writer.close()
        else:
            with self.lock:
                if self.current_file:
                    self.current_file.close()
                    self.current_file = None
        if self.warning_manager:
            self.warning_manager.close()


class Logger:
    """
    日志主类（单例）
    支持配置热重载、独立控制台窗口
    """
    _instance = None
    _lock = threading.Lock()

    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        if hasattr(self, '_initialized'):
            return
        self._initialized = False
        self.config = {
            'level': LogLevel.INFO,
            'console_enabled': True,
            'console_level': LogLevel.INFO,
            'console_color': True,
            'file_enabled': True,
            'file_level': LogLevel.DEBUG,
            'log_dir': 'logs',
            'async_enabled': False,
            'async_queue_size': 5000,
            'file_rotation': {
                'max_size_mb': 100,
                'retention_days': 30,
                'max_total_size_mb': 1024,
                'warning_separate': True,
                'by_date': True,
                'by_size': True,
            },
            'format': {
                'console': None,   # 预留，后续可自定义
                'file': None,
                'file_json': False,
            },
            'console_window': True,   # 是否打开独立控制台窗口
        }
        self.file_manager = None
        self.context_local = threading.local()
        self._console_process = None   # 独立控制台子进程
        self._config_reload_callback = None
        self._initialized = True

    def _apply_config(self):
        """
        根据当前 config 字典刷新日志系统状态
        支持增量更新，仅在必要参数变化时重建文件管理器
        """
        # 检查是否需要重建文件管理器
        need_recreate = False
        if self.config['file_enabled']:
            if self.file_manager is None:
                need_recreate = True
            else:
                # 比较关键参数
                old = self.file_manager
                if (old.async_enabled != self.config['async_enabled'] or
                    old.log_dir != Path(self.config['log_dir']) or
                    old.max_size_mb != self.config['file_rotation']['max_size_mb'] or
                    old.retention_days != self.config['file_rotation']['retention_days'] or
                    old.max_total_size_mb != self.config['file_rotation']['max_total_size_mb'] or
                    old.warning_separate != self.config['file_rotation']['warning_separate'] or
                    old.rotation_by_date != self.config['file_rotation']['by_date'] or
                    old.rotation_by_size != self.config['file_rotation']['by_size']):
                    need_recreate = True
        else:
            if self.file_manager is not None:
                need_recreate = True

        if need_recreate:
            # 优雅关闭旧管理器
            if self.file_manager:
                self.file_manager.close()
                self.file_manager = None
            if self.config['file_enabled']:
                self.file_manager = LogFileManager(
                    log_dir=self.config['log_dir'],
                    async_enabled=self.config['async_enabled'],
                    async_queue_size=self.config['async_queue_size'],
                    max_size_mb=self.config['file_rotation']['max_size_mb'],
                    retention_days=self.config['file_rotation']['retention_days'],
                    max_total_size_mb=self.config['file_rotation']['max_total_size_mb'],
                    warning_separate=self.config['file_rotation']['warning_separate'],
                    rotation_by_date=self.config['file_rotation']['by_date'],
                    rotation_by_size=self.config['file_rotation']['by_size'],
                )
            if self.config.get('console_window', True) and self.config['console_enabled']:
                self._stop_log_console()
                self._start_log_console()

        # 处理独立控制台窗口
        if self.config.get('console_window', True) and self.config['console_enabled']:
            self._start_log_console()
        else:
            self._stop_log_console()

    def _start_log_console(self):
        """启动独立控制台窗口，实时显示最新日志（tail -f 模式）"""
        if self._console_process is not None:
            return
        if not self.config['file_enabled']:
            return
        # 确定要显示的日志文件（优先显示当前日志文件）
        try:
            if self.file_manager and self.file_manager.current_file:
                log_file_path = self.file_manager.current_file.name
            else:
                # 查找最新的日志文件
                log_dir = Path(self.config['log_dir'])
                files = sorted(log_dir.glob("*.log"), key=lambda f: f.stat().st_mtime, reverse=True)
                if not files:
                    return
                log_file_path = str(files[0])
            # 使用 PowerShell 的 Get-Content -Wait 实现实时 tail
            cmd = [
                'powershell', '-Command',
                f"Get-Content -Path '{log_file_path}' -Wait"
            ]
            # 创建新进程，不继承标准输入，独立窗口
            self._console_process = subprocess.Popen(
                cmd,
                creationflags=subprocess.CREATE_NEW_CONSOLE if sys.platform == 'win32' else 0,
                stdin=subprocess.DEVNULL,
                stdout=None,
                stderr=None
            )
        except Exception as e:
            sys.stderr.write(f"启动日志控制台窗口失败: {e}\n")

    def _stop_log_console(self):
        """关闭独立控制台窗口"""
        if self._console_process:
            try:
                self._console_process.terminate()
                self._console_process.wait(timeout=2)
            except:
                self._console_process.kill()
            self._console_process = None

    def init(self, **kwargs):
        """手动配置日志系统"""
        # 更新配置
        if 'level' in kwargs:
            self.config['level'] = LogLevel.from_string(kwargs['level'])
        if 'console_enabled' in kwargs:
            self.config['console_enabled'] = kwargs['console_enabled']
        if 'console_level' in kwargs:
            self.config['console_level'] = LogLevel.from_string(kwargs['console_level'])
        if 'console_color' in kwargs:
            self.config['console_color'] = kwargs['console_color']
        if 'file_enabled' in kwargs:
            self.config['file_enabled'] = kwargs['file_enabled']
        if 'file_level' in kwargs:
            self.config['file_level'] = LogLevel.from_string(kwargs['file_level'])
        if 'log_dir' in kwargs:
            self.config['log_dir'] = kwargs['log_dir']
        if 'async_enabled' in kwargs:
            self.config['async_enabled'] = kwargs['async_enabled']
        if 'async_queue_size' in kwargs:
            self.config['async_queue_size'] = kwargs['async_queue_size']
        if 'console_window' in kwargs:
            self.config['console_window'] = kwargs['console_window']
        # 轮转参数
        rotation = self.config['file_rotation']
        if 'max_size_mb' in kwargs:
            rotation['max_size_mb'] = kwargs['max_size_mb']
        if 'retention_days' in kwargs:
            rotation['retention_days'] = kwargs['retention_days']
        if 'max_total_size_mb' in kwargs:
            rotation['max_total_size_mb'] = kwargs['max_total_size_mb']
        if 'warning_separate' in kwargs:
            rotation['warning_separate'] = kwargs['warning_separate']
        if 'rotation_by_date' in kwargs:
            rotation['by_date'] = kwargs['rotation_by_date']
        if 'rotation_by_size' in kwargs:
            rotation['by_size'] = kwargs['rotation_by_size']

        self._apply_config()

        if not hasattr(self, '_atexit_registered'):
            atexit.register(self.close)
            self._atexit_registered = True
        if not hasattr(self, '_excepthook_set'):
            sys.excepthook = self._global_exception_handler
            self._excepthook_set = True

    def init_from_config(self, config_manager=None):
        """从配置管理器加载配置，并注册热重载"""
        if config_manager is None and CONFIG_AVAILABLE:
            config_manager = config
        if config_manager is None:
            raise RuntimeError("配置管理器不可用")
        # 读取日志配置
        log_cfg = config_manager.get('log', {})
        self.init(
            level=log_cfg.get('level', 'INFO'),
            console_enabled=log_cfg.get('console', {}).get('enabled', True),
            console_level=log_cfg.get('console', {}).get('level', 'INFO'),
            console_color=log_cfg.get('console', {}).get('color', True),
            file_enabled=log_cfg.get('file', {}).get('enabled', True),
            file_level=log_cfg.get('file', {}).get('level', 'DEBUG'),
            log_dir=log_cfg.get('file', {}).get('path', 'logs'),
            async_enabled=log_cfg.get('performance', {}).get('async_enabled', False),
            async_queue_size=log_cfg.get('performance', {}).get('async_queue_size', 5000),
            console_window=log_cfg.get('console_window', True),
            max_size_mb=log_cfg.get('file', {}).get('rotation', {}).get('max_size_mb', 100),
            retention_days=log_cfg.get('file', {}).get('rotation', {}).get('retention_days', 30),
            max_total_size_mb=log_cfg.get('file', {}).get('rotation', {}).get('max_total_size_mb', 1024),
            warning_separate=log_cfg.get('file', {}).get('rotation', {}).get('warning_separate', True),
            rotation_by_date=log_cfg.get('file', {}).get('rotation', {}).get('by_date', True),
            rotation_by_size=log_cfg.get('file', {}).get('rotation', {}).get('by_size', True),
        )
        # 注册配置热重载回调
        def on_config_change(key, old, new):
            if key.startswith('log.'):
                self.reload_from_config(config_manager)
        if self._config_reload_callback is None:
            config_manager.watch(on_config_change)
            self._config_reload_callback = on_config_change

    def reload_from_config(self, config_manager=None):
        """热重载配置（增量更新）"""
        if config_manager is None and CONFIG_AVAILABLE:
            config_manager = config
        if config_manager is None:
            return
        log_cfg = config_manager.get('log', {})
        old_config = copy.deepcopy(self.config)
        # 更新各项配置
        self.config['level'] = LogLevel.from_string(log_cfg.get('level', 'INFO'))
        self.config['console_enabled'] = log_cfg.get('console', {}).get('enabled', True)
        self.config['console_level'] = LogLevel.from_string(log_cfg.get('console', {}).get('level', 'INFO'))
        self.config['console_color'] = log_cfg.get('console', {}).get('color', True)
        self.config['file_enabled'] = log_cfg.get('file', {}).get('enabled', True)
        self.config['file_level'] = LogLevel.from_string(log_cfg.get('file', {}).get('level', 'DEBUG'))
        new_log_dir = log_cfg.get('file', {}).get('path', 'logs')
        if self.config['log_dir'] != new_log_dir:
            self.config['log_dir'] = new_log_dir
        self.config['async_enabled'] = log_cfg.get('performance', {}).get('async_enabled', False)
        self.config['async_queue_size'] = log_cfg.get('performance', {}).get('async_queue_size', 5000)
        self.config['console_window'] = log_cfg.get('console_window', True)

        rotation = self.config['file_rotation']
        new_rotation = log_cfg.get('file', {}).get('rotation', {})
        rotation['max_size_mb'] = new_rotation.get('max_size_mb', 100)
        rotation['retention_days'] = new_rotation.get('retention_days', 30)
        rotation['max_total_size_mb'] = new_rotation.get('max_total_size_mb', 1024)
        rotation['warning_separate'] = new_rotation.get('warning_separate', True)
        rotation['by_date'] = new_rotation.get('by_date', True)
        rotation['by_size'] = new_rotation.get('by_size', True)

        self._apply_config()

    def _global_exception_handler(self, exc_type, exc_value, exc_traceback):
        self.critical(f"未捕获的异常: {exc_type.__name__}: {exc_value}", exc_info=True, _skip_frame=1)
        sys.__excepthook__(exc_type, exc_value, exc_traceback)

    def _get_call_info(self, skip_frames=2):
        try:
            import inspect
            frame = inspect.currentframe()
            for _ in range(skip_frames):
                if frame:
                    frame = frame.f_back
            if frame:
                return os.path.basename(frame.f_code.co_filename), frame.f_lineno
        except:
            pass
        return "unknown", 0

    def _format_console(self, level: LogLevel, msg: str, filename: str, lineno: int, **kwargs) -> str:
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        pid = os.getpid()
        tid = threading.get_native_id() if hasattr(threading, 'get_native_id') else threading.current_thread().ident
        base = f"{ts} | {pid} | {tid} | {filename}:{lineno} | {level.name} | {msg}"
        if kwargs:
            extra = " ".join(f"{k}={escape_kv_value(v)}" for k, v in kwargs.items())
            base += f" [{extra}]"
        if hasattr(self.context_local, 'context') and self.context_local.context:
            ctx = " ".join(f"{k}={escape_kv_value(v)}" for k, v in self.context_local.context.items())
            base += f" {{{ctx}}}"
        if self.config['console_enabled'] and self.config['console_color']:
            color = ConsoleColor.get_color(level)
            base = f"{color}{base}{ConsoleColor.RESET}"
        return base

    def _format_file(self, level: LogLevel, msg: str, filename: str, lineno: int, **kwargs) -> str:
        if self.config['format'].get('file_json', False):
            # JSON 格式输出
            import json
            record = {
                "time": datetime.now().isoformat(),
                "pid": os.getpid(),
                "tid": threading.get_native_id() if hasattr(threading, 'get_native_id') else threading.current_thread().ident,
                "file": filename,
                "line": lineno,
                "level": level.name,
                "msg": msg,
                **kwargs
            }
            if hasattr(self.context_local, 'context') and self.context_local.context:
                for k, v in self.context_local.context.items():
                    record[f"ctx_{k}"] = v
            return json.dumps(record, ensure_ascii=False) + "\n"
        else:
            ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            pid = os.getpid()
            tid = threading.get_native_id() if hasattr(threading, 'get_native_id') else threading.current_thread().ident
            parts = [
                f"time={escape_kv_value(ts)}",
                f"pid={pid}",
                f"tid={tid}",
                f"file={escape_kv_value(filename)}",
                f"line={lineno}",
                f"level={level.name}",
                f"msg={escape_kv_value(msg)}"
            ]
            for k, v in kwargs.items():
                parts.append(f"{k}={escape_kv_value(v)}")
            if hasattr(self.context_local, 'context') and self.context_local.context:
                for k, v in self.context_local.context.items():
                    parts.append(f"ctx_{k}={escape_kv_value(v)}")
            return " ".join(parts) + "\n"

    def _log(self, level: LogLevel, msg: str, exc_info: bool = False, skip_frames: int = 2, **kwargs):
        if level.value < self.config['level'].value:
            return
        filename, lineno = self._get_call_info(skip_frames)
        if exc_info:
            tb = traceback.format_exc()
            if tb and tb != "NoneType: None\n":
                kwargs['traceback'] = tb.strip()
        if self.config['console_enabled'] and level.value >= self.config['console_level'].value:
            print(self._format_console(level, msg, filename, lineno, **kwargs), flush=True)
        if self.config['file_enabled'] and level.value >= self.config['file_level'].value and self.file_manager:
            self.file_manager.write(self._format_file(level, msg, filename, lineno, **kwargs))

    def debug(self, msg: str, **kwargs): self._log(LogLevel.DEBUG, msg, False, 2, **kwargs)
    def info(self, msg: str, **kwargs): self._log(LogLevel.INFO, msg, False, 2, **kwargs)
    def warning(self, msg: str, **kwargs): self._log(LogLevel.WARNING, msg, False, 2, **kwargs)
    def error(self, msg: str, exc_info: bool = True, **kwargs): self._log(LogLevel.ERROR, msg, exc_info, 2, **kwargs)
    def critical(self, msg: str, exc_info: bool = True, **kwargs): self._log(LogLevel.CRITICAL, msg, exc_info, 2, **kwargs)

    def bind(self, **kwargs):
        if not hasattr(self.context_local, 'context'):
            self.context_local.context = {}
        self.context_local.context.update(kwargs)

    def unbind(self, *keys):
        if hasattr(self.context_local, 'context'):
            for k in keys:
                self.context_local.context.pop(k, None)

    def clear_context(self):
        if hasattr(self.context_local, 'context'):
            self.context_local.context.clear()

    def set_level(self, level: Union[str, LogLevel]):
        if isinstance(level, str):
            level = LogLevel.from_string(level)
        self.config['level'] = level

    def flush(self):
        if self.file_manager:
            self.file_manager.flush()

    def cleanup(self):
        if self.file_manager:
            self.file_manager.cleanup()

    def close(self):
        self.flush()
        self._stop_log_console()
        if self.file_manager:
            self.file_manager.close()


# 全局单例实例
_logger = Logger()

# 模块级便捷函数
def log_init(**kwargs): _logger.init(**kwargs)
def log_init_from_config(config_manager=None): _logger.init_from_config(config_manager)
def log_reload_config(config_manager=None): _logger.reload_from_config(config_manager)
def log_debug(msg: str, **kwargs): _logger.debug(msg, **kwargs)
def log_info(msg: str, **kwargs): _logger.info(msg, **kwargs)
def log_warning(msg: str, **kwargs): _logger.warning(msg, **kwargs)
def log_error(msg: str, exc_info=True, **kwargs): _logger.error(msg, exc_info=exc_info, **kwargs)
def log_critical(msg: str, exc_info=True, **kwargs): _logger.critical(msg, exc_info=exc_info, **kwargs)
def log_bind(**kwargs): _logger.bind(**kwargs)
def log_unbind(*keys): _logger.unbind(*keys)
def log_clear_context(): _logger.clear_context()
def log_set_level(level: str): _logger.set_level(level)
def log_flush(): _logger.flush()
def log_cleanup(): _logger.cleanup()
def log_close(): _logger.close()
def log_open_console(): _logger._start_log_console()   # 手动打开独立控制台窗口
def log_close_console(): _logger._stop_log_console()