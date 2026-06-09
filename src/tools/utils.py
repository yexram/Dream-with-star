# src/tools/utils.py
"""
通用辅助工具集
提供时间、文件、网络、验证、加密、重试、线程池等常用功能
所有功能均使用配置管理器和日志系统
"""

import os
import sys
import re
import json
import hashlib
import hmac
import threading
import queue
import time
import socket
import struct
import random
import gzip
import zipfile
import tempfile
import shutil
import asyncio
from pathlib import Path
from typing import Any, Dict, List, Optional, Union, Callable, Tuple, Iterable
from functools import wraps
from datetime import datetime, timedelta, timezone
import urllib.request
import urllib.error

# 第三方依赖（可选）
try:
    import requests
    REQUESTS_AVAILABLE = True
except ImportError:
    REQUESTS_AVAILABLE = False

try:
    import yaml
    YAML_AVAILABLE = True
except ImportError:
    YAML_AVAILABLE = False

try:
    import bcrypt
    BCRYPT_AVAILABLE = True
except ImportError:
    BCRYPT_AVAILABLE = False

try:
    import argon2
    ARGON2_AVAILABLE = True
except ImportError:
    ARGON2_AVAILABLE = False

try:
    from watchdog.observers import Observer
    from watchdog.events import FileSystemEventHandler
    WATCHDOG_AVAILABLE = True
except ImportError:
    WATCHDOG_AVAILABLE = False

# 内部依赖
from src.tools.logger import log_debug, log_info, log_warning, log_error
from src.tools.config_manager import config as global_config


# ==================== A. 时间工具 ====================
class TimeHelper:
    """时间工具类"""
    
    _timezone_cache = {}
    
    @classmethod
    def timestamp_ms(cls) -> int:
        """当前毫秒时间戳"""
        return int(time.time() * 1000)
    
    @classmethod
    def timestamp_sec(cls) -> int:
        """当前秒时间戳"""
        return int(time.time())
    
    @classmethod
    def from_timestamp(cls, timestamp: Union[int, float], unit: str = "sec") -> datetime:
        """时间戳转datetime"""
        if unit == "ms":
            timestamp = timestamp / 1000.0
        return datetime.fromtimestamp(timestamp)
    
    @classmethod
    def format_chinese(cls, dt: Optional[datetime] = None) -> str:
        """中文日期格式化"""
        if dt is None:
            dt = datetime.now()
        return f"{dt.year}年{dt.month:02d}月{dt.day:02d}日 {dt.hour:02d}:{dt.minute:02d}:{dt.second:02d}"
    
    @classmethod
    def time_period(cls, dt: Optional[datetime] = None) -> str:
        """时段判断：凌晨/上午/中午/下午/傍晚/晚上"""
        if dt is None:
            dt = datetime.now()
        hour = dt.hour
        if 5 <= hour < 8:
            return "凌晨"
        elif 8 <= hour < 12:
            return "上午"
        elif 12 <= hour < 13:
            return "中午"
        elif 13 <= hour < 18:
            return "下午"
        elif 18 <= hour < 20:
            return "傍晚"
        else:
            return "晚上"
    
    @classmethod
    def to_timezone(cls, dt: datetime, target_tz: str) -> datetime:
        """时区转换，target_tz 如 'Asia/Shanghai', 'America/New_York'"""
        try:
            import zoneinfo
            tz = zoneinfo.ZoneInfo(target_tz)
            return dt.astimezone(tz)
        except ImportError:
            try:
                import pytz  # type: ignore
                tz = pytz.timezone(target_tz)
                return dt.astimezone(tz)
            except ImportError:
                log_error(f"时区转换失败，未安装zoneinfo或pytz，目标时区{target_tz}")
                return dt
    
    @classmethod
    def is_workday(cls, dt: Optional[datetime] = None, holidays: Optional[List[str]] = None) -> bool:
        """判断工作日（周一至周五，并可排除假期）"""
        if dt is None:
            dt = datetime.now()
        if dt.weekday() >= 5:
            return False
        if holidays:
            date_str = dt.strftime("%Y-%m-%d")
            if date_str in holidays:
                return False
        return True
    
    @classmethod
    def parse_natural(cls, text: str, base: Optional[datetime] = None) -> Optional[datetime]:
        """自然语言解析基础版（今天/明天/后天 + 时间）"""
        if base is None:
            base = datetime.now().replace(second=0, microsecond=0)
        text = text.strip().lower()
        days_offset = 0
        if text.startswith("明天"):
            days_offset = 1
            text = text[2:]
        elif text.startswith("后天"):
            days_offset = 2
            text = text[2:]
        elif text.startswith("今天"):
            text = text[2:]
        match = re.search(r'(\d{1,2}):(\d{2})', text)
        if match:
            hour = int(match.group(1))
            minute = int(match.group(2))
            result = base + timedelta(days=days_offset)
            result = result.replace(hour=hour, minute=minute)
            return result
        return None
    
    @classmethod
    def format_duration(cls, seconds: int, lang: str = "zh") -> str:
        """时长国际化格式化"""
        if seconds < 0:
            return ""
        parts = []
        days = seconds // 86400
        if days:
            parts.append(f"{days}天" if lang == "zh" else f"{days}d")
            seconds %= 86400
        hours = seconds // 3600
        if hours:
            parts.append(f"{hours}小时" if lang == "zh" else f"{hours}h")
            seconds %= 3600
        minutes = seconds // 60
        if minutes:
            parts.append(f"{minutes}分钟" if lang == "zh" else f"{minutes}m")
        secs = seconds % 60
        if secs or not parts:
            parts.append(f"{secs}秒" if lang == "zh" else f"{secs}s")
        return "".join(parts)


# ==================== B. 文件工具 ====================
class FileHelper:
    """文件工具类"""
    
    @staticmethod
    def safe_write_text(path: Union[str, Path], content: str, encoding: str = "utf-8"):
        """安全写入文本（临时文件+原子替换）"""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".tmp")
        try:
            with os.fdopen(fd, 'w', encoding=encoding) as f:
                f.write(content)
            os.replace(tmp, path)
        except Exception:
            os.unlink(tmp)
            raise
    
    @staticmethod
    def safe_write_binary(path: Union[str, Path], data: bytes):
        """安全写入二进制"""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".tmp")
        try:
            with os.fdopen(fd, 'wb') as f:
                f.write(data)
            os.replace(tmp, path)
        except Exception:
            os.unlink(tmp)
            raise
    
    @staticmethod
    def read_text(path: Union[str, Path], encoding: str = "utf-8") -> str:
        """读取文本文件"""
        with open(path, 'r', encoding=encoding) as f:
            return f.read()
    
    @staticmethod
    def read_binary(path: Union[str, Path]) -> bytes:
        """读取二进制文件"""
        with open(path, 'rb') as f:
            return f.read()
    
    @staticmethod
    def get_meta(path: Union[str, Path]) -> Dict:
        """获取文件元信息"""
        p = Path(path)
        stat = p.stat()
        return {
            "size": stat.st_size,
            "mtime": datetime.fromtimestamp(stat.st_mtime),
            "ctime": datetime.fromtimestamp(stat.st_ctime),
            "ext": p.suffix,
            "owner": stat.st_uid if hasattr(stat, 'st_uid') else None
        }
    
    @staticmethod
    def copy(src: Union[str, Path], dst: Union[str, Path], backup: bool = False, backup_dir: Optional[Union[str, Path]] = None):
        """复制文件，可选备份目标位置"""
        src = Path(src)
        dst = Path(dst)
        if backup and dst.exists():
            bk_dir = Path(backup_dir) if backup_dir else dst.parent / "backup"
            bk_dir.mkdir(parents=True, exist_ok=True)
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            backup_path = bk_dir / f"{dst.name}.{timestamp}.bak"
            shutil.copy2(dst, backup_path)
        shutil.copy2(src, dst)
    
    @staticmethod
    def move(src: Union[str, Path], dst: Union[str, Path]):
        """移动文件，自动处理跨设备"""
        shutil.move(str(src), str(dst))
    
    @staticmethod
    def delete(path: Union[str, Path], backup: bool = False, backup_dir: Optional[Union[str, Path]] = None):
        """删除文件，可选备份"""
        p = Path(path)
        if not p.exists():
            return
        if backup:
            bk_dir = Path(backup_dir) if backup_dir else p.parent / "backup"
            bk_dir.mkdir(parents=True, exist_ok=True)
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            backup_path = bk_dir / f"{p.name}.{timestamp}.delbak"
            shutil.move(str(p), str(backup_path))
        else:
            p.unlink()
    
    @staticmethod
    def load_config(path: Union[str, Path]) -> Dict:
        """自动识别并读取配置文件（JSON/YAML/INI/TOML）"""
        path = Path(path)
        suffix = path.suffix.lower()
        if suffix == ".json":
            with open(path, 'r', encoding='utf-8') as f:
                return json.load(f)
        elif suffix in (".yaml", ".yml"):
            if YAML_AVAILABLE:
                with open(path, 'r', encoding='utf-8') as f:
                    return yaml.safe_load(f)
            else:
                raise ImportError("需要安装 PyYAML 来读取 YAML 文件")
        elif suffix == ".ini":
            import configparser
            cp = configparser.ConfigParser()
            cp.read(path, encoding='utf-8')
            return {s: dict(cp.items(s)) for s in cp.sections()}
        elif suffix == ".toml":
            try:
                import tomllib
                with open(path, 'rb') as f:
                    return tomllib.load(f)
            except ImportError:
                try:
                    import toml  # type: ignore
                    with open(path, 'r', encoding='utf-8') as f:
                        return toml.load(f)
                except ImportError:
                    raise ImportError("需要安装 tomli 或 toml 来读取 TOML 文件")
        else:
            raise ValueError(f"不支持的文件格式: {suffix}")
    
    @staticmethod
    def write_config(path: Union[str, Path], data: Dict, format: Optional[str] = None) -> bool:
        """
        将字典写入配置文件，格式根据扩展名或 format 参数决定。
        支持 json, yaml, toml, ini。
        """
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        
        if format is None:
            suffix = path.suffix.lower()
            if suffix == ".json":
                format = "json"
            elif suffix in (".yaml", ".yml"):
                format = "yaml"
            elif suffix == ".toml":
                format = "toml"
            elif suffix == ".ini":
                format = "ini"
            else:
                raise ValueError(f"无法自动识别格式，请指定 format 参数: {suffix}")
        
        try:
            if format == "json":
                with open(path, 'w', encoding='utf-8') as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
            elif format == "yaml":
                if not YAML_AVAILABLE:
                    raise ImportError("需要安装 PyYAML")
                with open(path, 'w', encoding='utf-8') as f:
                    yaml.dump(data, f, allow_unicode=True)
            elif format == "toml":
                try:
                    import tomli_w
                    with open(path, 'wb') as f:
                        tomli_w.dump(data, f)
                except ImportError:
                    try:
                        import toml  # type: ignore
                        with open(path, 'w', encoding='utf-8') as f:
                            toml.dump(data, f)
                    except ImportError:
                        raise ImportError("需要安装 tomli_w 或 toml")
            elif format == "ini":
                import configparser
                cp = configparser.ConfigParser()
                for section, values in data.items():
                    cp[section] = values
                with open(path, 'w', encoding='utf-8') as f:
                    cp.write(f)
            else:
                raise ValueError(f"不支持的格式: {format}")
            log_info(f"配置写入成功: {path}")
            return True
        except Exception as e:
            log_error(f"配置写入失败: {e}")
            return False
    
    @staticmethod
    def watch_changes(path: Union[str, Path], callback: Callable[[str, str], None], recursive: bool = False):
        """
        监听文件变化（基于 watchdog 库）
        callback 接收事件类型和路径字符串
        """
        if not WATCHDOG_AVAILABLE:
            raise ImportError("watchdog 库未安装，无法监听文件变化")
        
        class Handler(FileSystemEventHandler):
            def on_any_event(self, event):
                if event.is_directory:
                    return
                callback(event.event_type, event.src_path)
        
        observer = Observer()
        observer.schedule(Handler(), str(path), recursive=recursive)
        observer.start()
        return observer
    
    @staticmethod
    def read_compressed(path: Union[str, Path]) -> bytes:
        """透明读取压缩文件（.gz, .zip 中单个文件）"""
        path = Path(path)
        suffix = path.suffix.lower()
        if suffix == ".gz":
            with gzip.open(path, 'rb') as f:
                return f.read()
        elif suffix == ".zip":
            with zipfile.ZipFile(path, 'r') as zf:
                name = zf.namelist()[0]
                return zf.read(name)
        else:
            with open(path, 'rb') as f:
                return f.read()
    
    @staticmethod
    def write_compressed(path: Union[str, Path], data: bytes, compresslevel: int = 6):
        """写入 gzip 压缩文件"""
        path = Path(path)
        if path.suffix.lower() != ".gz":
            path = path.with_suffix(path.suffix + ".gz")
        with gzip.open(path, 'wb', compresslevel=compresslevel) as f:
            f.write(data)


# ==================== C. 网络检测（即时探测）====================
class NetworkChecker:
    """即时网络探测工具（同步/异步）"""
    
    @staticmethod
    def is_online(dns_servers: Optional[List[str]] = None, timeout: float = 3) -> bool:
        """检查是否联网（多DNS并行）"""
        if dns_servers is None:
            dns_servers = global_config.get("utils.network_checker.dns_servers", ["8.8.8.8", "114.114.114.114", "1.1.1.1"])
        timeout = timeout or global_config.get("utils.network_checker.timeout", 3)
        
        def check(ip):
            try:
                socket.create_connection((ip, 53), timeout=timeout)
                return True
            except:
                return False
        
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(dns_servers)) as executor:
            futures = [executor.submit(check, ip) for ip in dns_servers]
            for future in concurrent.futures.as_completed(futures):
                if future.result():
                    return True
        return False
    
    @staticmethod
    def public_ip() -> Optional[str]:
        """获取公网IP（使用多个API）"""
        apis = [
            "https://api.ipify.org",
            "https://api.my-ip.io/ip",
            "https://checkip.amazonaws.com"
        ]
        for api in apis:
            try:
                with urllib.request.urlopen(api, timeout=5) as resp:
                    ip = resp.read().decode().strip()
                    if re.match(r'^\d+\.\d+\.\d+\.\d+$', ip):
                        return ip
            except:
                continue
        return None
    
    @staticmethod
    def ping(host: str, timeout: float = 2, count: int = 1) -> Tuple[bool, Optional[float]]:
        """Ping 主机，返回 (是否成功, RTT秒数)，优先 ICMP 降级 TCP 80"""
        try:
            icmp_socket = socket.socket(socket.AF_INET, socket.SOCK_RAW, socket.IPPROTO_ICMP)
            icmp_socket.settimeout(timeout)
        except PermissionError:
            return NetworkChecker._tcp_ping(host, timeout, count)
        except Exception:
            return False, None
        
        try:
            start = time.time()
            packet = struct.pack('>BBHHH', 8, 0, 0, 12345, 1)
            checksum = 0
            packet = struct.pack('>BBHHH', 8, 0, checksum, 12345, 1)
            icmp_socket.sendto(packet, (host, 0))
            icmp_socket.recvfrom(1024)
            rtt = (time.time() - start)
            icmp_socket.close()
            return True, rtt
        except socket.timeout:
            return False, None
        except Exception as e:
            log_error(f"ICMP Ping 失败: {e}")
            return False, None
        finally:
            try:
                icmp_socket.close()
            except:
                pass
    
    @staticmethod
    def _tcp_ping(host: str, timeout: float, count: int = 1) -> Tuple[bool, Optional[float]]:
        """使用 TCP 连接测试延迟（端口 80）"""
        try:
            start = time.time()
            with socket.create_connection((host, 80), timeout=timeout):
                rtt = time.time() - start
                return True, rtt
        except:
            return False, None
    
    @staticmethod
    def check_port(host: str, port: int, timeout: float = 3) -> bool:
        """TCP 端口连通性检测"""
        try:
            with socket.create_connection((host, port), timeout=timeout):
                return True
        except:
            return False
    
    @staticmethod
    def measure_rtt_http(host: str, timeout: float = 3) -> Optional[float]:
        """通过 HTTP HEAD 请求测量 RTT"""
        if not REQUESTS_AVAILABLE:
            return None
        try:
            url = host if host.startswith("http") else f"http://{host}"
            start = time.time()
            requests.head(url, timeout=timeout)
            return time.time() - start
        except:
            return None
    
    @staticmethod
    async def async_is_online(dns_servers: Optional[List[str]] = None, timeout: float = 3) -> bool:
        """异步版本 is_online"""
        if dns_servers is None:
            dns_servers = global_config.get("utils.network_checker.dns_servers", ["8.8.8.8", "114.114.114.114", "1.1.1.1"])
        
        async def check(ip):
            loop = asyncio.get_running_loop()
            try:
                await loop.run_in_executor(None, socket.create_connection, (ip, 53), timeout)
                return True
            except:
                return False
        
        tasks = [check(ip) for ip in dns_servers]
        results = await asyncio.gather(*tasks)
        return any(results)
    
    @staticmethod
    def check_hosts(targets: List[Dict], timeout: float = 3.0, concurrency: int = 10) -> List[Dict]:
        """
        批量健康检查（并发检测多个主机/端口）
        targets 格式: [{"host": "8.8.8.8", "port": 53, "type": "tcp"}, {"host": "google.com", "type": "ping"}]
        type 可选 "tcp" (默认) 或 "ping"
        返回列表，每个元素包含 host, port, alive, rtt (或 error)
        """
        import concurrent.futures
        
        def check_one(target):
            host = target["host"]
            port = target.get("port", 80)
            probe_type = target.get("type", "tcp")
            result = {"host": host, "port": port, "type": probe_type}
            try:
                if probe_type == "ping":
                    alive, rtt = NetworkChecker.ping(host, timeout=timeout)
                else:  # tcp
                    start = time.time()
                    with socket.create_connection((host, port), timeout=timeout):
                        rtt = time.time() - start
                    alive = True
                result["alive"] = alive
                if alive:
                    result["rtt"] = rtt
                else:
                    result["error"] = "connection failed"
            except Exception as e:
                result["alive"] = False
                result["error"] = str(e)
            return result
        
        results = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as executor:
            futures = [executor.submit(check_one, t) for t in targets]
            for future in concurrent.futures.as_completed(futures):
                results.append(future.result())
        return results


# ==================== D. 数据验证器 ====================
class Validator:
    """数据验证器"""
    
    _rules = {}
    
    @classmethod
    def register_rule(cls, name: str, func: Callable[[Any], Tuple[bool, str]]):
        """注册自定义验证规则"""
        cls._rules[name] = func
    
    @classmethod
    def validate(cls, value: Any, rules: List[Union[str, Tuple[str, Any]]]) -> List[str]:
        """对单个值应用规则列表"""
        errors = []
        for rule in rules:
            if isinstance(rule, str):
                rule_name = rule
                rule_param = None
            else:
                rule_name, rule_param = rule
            if rule_name in cls._rules:
                ok, err = cls._rules[rule_name](value, rule_param) if rule_param else cls._rules[rule_name](value)
            else:
                ok, err = cls._builtin_validate(rule_name, value, rule_param)
            if not ok:
                errors.append(err)
        return errors
    
    @classmethod
    def _builtin_validate(cls, rule_name: str, value: Any, param=None) -> Tuple[bool, str]:
        if rule_name == "email":
            pattern = r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$'
            return bool(re.match(pattern, str(value))), "无效的邮箱地址"
        elif rule_name == "mobile":
            pattern = r'^1[3-9]\d{9}$'
            return bool(re.match(pattern, str(value))), "无效的手机号码"
        elif rule_name == "idcard":
            return cls._validate_idcard(str(value)), "无效的身份证号码"
        elif rule_name == "url":
            pattern = r'^https?://\S+$'
            return bool(re.match(pattern, str(value))), "无效的URL"
        elif rule_name == "ipv4":
            pattern = r'^(\d{1,3}\.){3}\d{1,3}$'
            if not re.match(pattern, str(value)):
                return False, "无效的IPv4地址"
            parts = str(value).split('.')
            for p in parts:
                if int(p) > 255:
                    return False, "无效的IPv4地址"
            return True, ""
        elif rule_name == "ipv6":
            try:
                socket.inet_pton(socket.AF_INET6, str(value))
                return True, ""
            except:
                return False, "无效的IPv6地址"
        elif rule_name == "uuid":
            pattern = r'^[0-9a-f]{8}-([0-9a-f]{4}-){3}[0-9a-f]{12}$'
            return bool(re.match(pattern, str(value).lower())), "无效的UUID"
        elif rule_name == "date":
            try:
                datetime.strptime(str(value), "%Y-%m-%d")
                return True, ""
            except:
                return False, "无效的日期格式（需YYYY-MM-DD）"
        elif rule_name == "luhn":
            return cls._luhn_check(str(value)), "信用卡号无效"
        elif rule_name == "min_len":
            return len(str(value)) >= param, f"长度不能小于{param}"
        elif rule_name == "max_len":
            return len(str(value)) <= param, f"长度不能大于{param}"
        elif rule_name == "regex":
            return bool(re.match(param, str(value))), f"不符合正则表达式{param}"
        else:
            return False, f"未知规则{rule_name}"
    
    @staticmethod
    def _validate_idcard(idnum: str) -> bool:
        if len(idnum) == 18:
            factors = [7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2]
            check_codes = ['1', '0', 'X', '9', '8', '7', '6', '5', '4', '3', '2']
            try:
                sum_val = sum(int(idnum[i]) * factors[i] for i in range(17))
                expected = check_codes[sum_val % 11]
                if expected != idnum[17].upper():
                    log_debug(f"身份证校验失败: 期望 {expected}, 实际 {idnum[17].upper()}")
                    return False
                return True
            except Exception as e:
                log_error(f"身份证校验异常: {e}")
                return False
        elif len(idnum) == 15:
            return idnum.isdigit()
        return False
    
    @staticmethod
    def _luhn_check(card_num: str) -> bool:
        digits = [int(c) for c in card_num if c.isdigit()]
        if len(digits) < 13:
            return False
        odd_sum = sum(digits[-1::-2])
        even_sum = sum(sum(divmod(d * 2, 10)) for d in digits[-2::-2])
        return (odd_sum + even_sum) % 10 == 0
    
    @classmethod
    def batch_validate(cls, data: Dict[str, Any], rules: Dict[str, List]) -> Dict[str, List[str]]:
        errors = {}
        for field, field_rules in rules.items():
            value = data.get(field)
            field_errors = cls.validate(value, field_rules)
            if field_errors:
                errors[field] = field_errors
        return errors
    
    @classmethod
    def convert(cls, value: Any, target_type: str) -> Any:
        if target_type == "int":
            return int(value)
        elif target_type == "float":
            return float(value)
        elif target_type == "bool":
            if isinstance(value, bool):
                return value
            return value.lower() in ('true', '1', 'yes')
        elif target_type == "str":
            return str(value)
        else:
            return value


# ==================== D1. 链式校验器 ====================
class ValidatorChain:
    """链式校验器，支持流畅接口组合多个校验规则（收集所有错误）"""
    
    _default_lang = "zh"
    
    def __init__(self, value: Any, lang: str = None):
        self._value = value
        self._errors: List[str] = []
        self._lang = lang or self._default_lang
    
    @classmethod
    def set_language(cls, lang: str):
        """设置全局语言（zh/en）"""
        cls._default_lang = lang
    
    def not_empty(self, msg: Optional[str] = None) -> "ValidatorChain":
        if self._value is None or (isinstance(self._value, str) and not self._value.strip()):
            self._errors.append(msg or ("值不能为空" if self._lang == "zh" else "Value cannot be empty"))
        return self
    
    def is_email(self, msg: Optional[str] = None) -> "ValidatorChain":
        if Validator.validate(self._value, ["email"]):  # 非空列表表示错误
            self._errors.append(msg or ("无效的邮箱地址" if self._lang == "zh" else "Invalid email address"))
        return self
    
    def is_phone(self, msg: Optional[str] = None) -> "ValidatorChain":
        if Validator.validate(self._value, ["mobile"]):
            self._errors.append(msg or ("无效的手机号码" if self._lang == "zh" else "Invalid phone number"))
        return self
    
    def is_url(self, msg: Optional[str] = None) -> "ValidatorChain":
        if Validator.validate(self._value, ["url"]):
            self._errors.append(msg or ("无效的URL" if self._lang == "zh" else "Invalid URL"))
        return self
    
    def is_ipv4(self, msg: Optional[str] = None) -> "ValidatorChain":
        if Validator.validate(self._value, ["ipv4"]):
            self._errors.append(msg or ("无效的IPv4地址" if self._lang == "zh" else "Invalid IPv4 address"))
        return self
    
    def is_uuid(self, msg: Optional[str] = None) -> "ValidatorChain":
        if Validator.validate(self._value, ["uuid"]):
            self._errors.append(msg or ("无效的UUID" if self._lang == "zh" else "Invalid UUID"))
        return self
    
    def is_date(self, fmt: str = "%Y-%m-%d", msg: Optional[str] = None) -> "ValidatorChain":
        try:
            datetime.strptime(str(self._value), fmt)
        except:
            self._errors.append(msg or (f"无效的日期格式（需{fmt}）" if self._lang == "zh" else f"Invalid date format (need {fmt})"))
        return self
    
    def min_len(self, length: int, msg: Optional[str] = None) -> "ValidatorChain":
        val_str = str(self._value)
        if len(val_str) < length:
            self._errors.append(msg or (f"长度不能小于{length}" if self._lang == "zh" else f"Length must be at least {length}"))
        return self
    
    def max_len(self, length: int, msg: Optional[str] = None) -> "ValidatorChain":
        val_str = str(self._value)
        if len(val_str) > length:
            self._errors.append(msg or (f"长度不能大于{length}" if self._lang == "zh" else f"Length must be at most {length}"))
        return self
    
    def regex(self, pattern: str, msg: Optional[str] = None) -> "ValidatorChain":
        if not re.match(pattern, str(self._value)):
            self._errors.append(msg or (f"不符合正则表达式{pattern}" if self._lang == "zh" else f"Does not match regex {pattern}"))
        return self
    
    def custom(self, func: Callable[[Any], bool], msg: str) -> "ValidatorChain":
        if not func(self._value):
            self._errors.append(msg)
        return self
    
    def result(self) -> Tuple[bool, List[str]]:
        """返回 (是否通过, 错误列表)"""
        return len(self._errors) == 0, self._errors
    
    def raise_if_invalid(self) -> None:
        """校验失败时抛出 ValueError，包含所有错误信息"""
        ok, errors = self.result()
        if not ok:
            raise ValueError("; ".join(errors))


# ==================== E. 加密哈希 ====================
class Hasher:
    """加密哈希工具"""
    
    @staticmethod
    def file_hash(path: Union[str, Path], algorithm: str = "sha256", chunk_size: int = 8192) -> str:
        hasher = hashlib.new(algorithm)
        with open(path, 'rb') as f:
            while chunk := f.read(chunk_size):
                hasher.update(chunk)
        return hasher.hexdigest()
    
    @staticmethod
    def string_hash(data: str, algorithm: str = "sha256") -> str:
        hasher = hashlib.new(algorithm)
        hasher.update(data.encode('utf-8'))
        return hasher.hexdigest()
    
    @staticmethod
    def hash_stream(chunk_generator: Iterable[bytes], algorithm: str = "sha256") -> str:
        hasher = hashlib.new(algorithm)
        for chunk in chunk_generator:
            hasher.update(chunk)
        return hasher.hexdigest()
    
    @staticmethod
    def password_hash(password: str, method: str = "bcrypt") -> str:
        if method == "bcrypt":
            if not BCRYPT_AVAILABLE:
                raise ImportError("bcrypt 未安装")
            salt = bcrypt.gensalt()
            return bcrypt.hashpw(password.encode(), salt).decode()
        elif method == "argon2":
            if not ARGON2_AVAILABLE:
                raise ImportError("argon2-cffi 未安装")
            ph = argon2.PasswordHasher()
            return ph.hash(password)
        else:
            raise ValueError(f"不支持的哈希方法: {method}")
    
    @staticmethod
    def password_verify(password: str, hashed: str, method: str = "bcrypt") -> bool:
        if method == "bcrypt":
            if not BCRYPT_AVAILABLE:
                return False
            return bcrypt.checkpw(password.encode(), hashed.encode())
        elif method == "argon2":
            if not ARGON2_AVAILABLE:
                return False
            ph = argon2.PasswordHasher()
            try:
                ph.verify(hashed, password)
                return True
            except:
                return False
        else:
            return False
    
    @staticmethod
    def hmac_sign(data: Union[str, bytes], key: Union[str, bytes], algorithm: str = "sha256") -> str:
        if isinstance(data, str):
            data = data.encode()
        if isinstance(key, str):
            key = key.encode()
        h = hmac.new(key, data, getattr(hashlib, algorithm))
        return h.hexdigest()
    
    @staticmethod
    def hmac_verify(data: Union[str, bytes], signature: str, key: Union[str, bytes], algorithm: str = "sha256") -> bool:
        expected = Hasher.hmac_sign(data, key, algorithm)
        return hmac.compare_digest(expected, signature)
    
    @staticmethod
    def multi_hash_file(path: Union[str, Path], algorithms: List[str] = None) -> Dict[str, str]:
        if algorithms is None:
            algorithms = ["md5", "sha1", "sha256"]
        hashers = {alg: hashlib.new(alg) for alg in algorithms}
        with open(path, 'rb') as f:
            while chunk := f.read(8192):
                for h in hashers.values():
                    h.update(chunk)
        return {alg: h.hexdigest() for alg, h in hashers.items()}


# ==================== F. 流式哈希类 ====================
class StreamingHasher:
    """
    流式哈希器，支持逐步更新数据并最终获取哈希值。
    使用方式：hasher = StreamingHasher('sha256'); hasher.update(b'data'); hash = hasher.hexdigest()
    """
    def __init__(self, algorithm: str = "sha256"):
        self._hasher = hashlib.new(algorithm)
        self._algorithm = algorithm
    
    def update(self, data: bytes) -> None:
        """更新要哈希的数据"""
        self._hasher.update(data)
    
    def hexdigest(self) -> str:
        """获取最终哈希值（十六进制字符串）"""
        return self._hasher.hexdigest()
    
    def digest(self) -> bytes:
        """获取最终哈希值（字节）"""
        return self._hasher.digest()
    
    def copy(self) -> "StreamingHasher":
        """复制当前哈希器状态"""
        new = StreamingHasher(self._algorithm)
        new._hasher = self._hasher.copy()
        return new


# ==================== G. 重试装饰器（增强：条件 + 熔断器）====================
class RetryContext:
    def __init__(self):
        self.total_attempts = 0
        self.success_count = 0
        self.failure_count = 0
        self.lock = threading.Lock()
    
    def record_attempt(self, success: bool):
        with self.lock:
            self.total_attempts += 1
            if success:
                self.success_count += 1
            else:
                self.failure_count += 1
    
    def get_stats(self):
        with self.lock:
            return {
                "total": self.total_attempts,
                "success": self.success_count,
                "failure": self.failure_count,
                "success_rate": self.success_count / self.total_attempts if self.total_attempts else 0
            }


_retry_stats = RetryContext()
_circuit_breakers: Dict[str, Dict] = {}
_circuit_lock = threading.Lock()

def _get_circuit_state(func_name: str):
    with _circuit_lock:
        state = _circuit_breakers.get(func_name, {"failures": 0, "open_until": 0})
        return state

def _update_circuit_state(func_name: str, success: bool, threshold: int, recovery_timeout: float):
    with _circuit_lock:
        state = _circuit_breakers.get(func_name, {"failures": 0, "open_until": 0})
        if success:
            state["failures"] = 0
            state["open_until"] = 0
        else:
            state["failures"] = state.get("failures", 0) + 1
            if state["failures"] >= threshold:
                state["open_until"] = time.time() + recovery_timeout
        _circuit_breakers[func_name] = state
        return state


def retry(
    max_attempts: int = None,
    delay: float = None,
    backoff_factor: float = None,
    jitter: bool = None,
    exceptions: Union[type, Tuple[type, ...]] = Exception,
    on_retry: Optional[Callable[[Exception, int], None]] = None,
    condition: Optional[Callable[[Exception], bool]] = None,
    use_circuit_breaker: bool = False,
    circuit_failure_threshold: int = 5,
    circuit_recovery_timeout: float = 60.0
):
    """
    增强版重试装饰器
    :param condition: 自定义重试条件，返回 True 则重试，False 则立即抛出异常
    :param use_circuit_breaker: 是否启用熔断器
    :param circuit_failure_threshold: 连续失败多少次后开启熔断
    :param circuit_recovery_timeout: 熔断持续时间（秒）
    """
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            attempts = max_attempts or global_config.get("utils.retry.default_max_attempts", 3)
            base_delay = delay or global_config.get("utils.retry.default_delay", 1.0)
            factor = backoff_factor or global_config.get("utils.retry.backoff_factor", 2.0)
            use_jitter = jitter if jitter is not None else global_config.get("utils.retry.jitter", True)
            
            func_name = f"{func.__module__}.{func.__qualname__}"
            
            # 熔断器检查
            if use_circuit_breaker:
                state = _get_circuit_state(func_name)
                if state["open_until"] > time.time():
                    raise RuntimeError(f"熔断器开启，函数 {func_name} 暂时不可用 (剩余 {state['open_until'] - time.time():.1f} 秒)")
            
            last_exc = None
            for i in range(attempts):
                try:
                    result = func(*args, **kwargs)
                    _retry_stats.record_attempt(True)
                    if use_circuit_breaker:
                        _update_circuit_state(func_name, True, circuit_failure_threshold, circuit_recovery_timeout)
                    return result
                except exceptions as e:
                    last_exc = e
                    _retry_stats.record_attempt(False)
                    if on_retry:
                        on_retry(e, i + 1)
                    # 条件检查：如果条件不满足，则立即停止重试
                    if condition is not None and not condition(e):
                        break
                    if i == attempts - 1:
                        break
                    wait = base_delay * (factor ** i)
                    if use_jitter:
                        wait = wait * (0.5 + random.random())
                    time.sleep(wait)
            
            if use_circuit_breaker:
                _update_circuit_state(func_name, False, circuit_failure_threshold, circuit_recovery_timeout)
            raise last_exc
        return wrapper
    return decorator


def async_retry(
    max_attempts: int = None,
    delay: float = None,
    backoff_factor: float = None,
    jitter: bool = None,
    exceptions: Union[type, Tuple[type, ...]] = Exception,
    on_retry: Optional[Callable[[Exception, int], None]] = None,
    condition: Optional[Callable[[Exception], bool]] = None,
    use_circuit_breaker: bool = False,
    circuit_failure_threshold: int = 5,
    circuit_recovery_timeout: float = 60.0
):
    """异步版本重试装饰器，参数同 retry"""
    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            attempts = max_attempts or global_config.get("utils.retry.default_max_attempts", 3)
            base_delay = delay or global_config.get("utils.retry.default_delay", 1.0)
            factor = backoff_factor or global_config.get("utils.retry.backoff_factor", 2.0)
            use_jitter = jitter if jitter is not None else global_config.get("utils.retry.jitter", True)
            
            func_name = f"{func.__module__}.{func.__qualname__}"
            
            if use_circuit_breaker:
                state = _get_circuit_state(func_name)
                if state["open_until"] > time.time():
                    raise RuntimeError(f"熔断器开启，函数 {func_name} 暂时不可用")
            
            last_exc = None
            for i in range(attempts):
                try:
                    result = await func(*args, **kwargs)
                    _retry_stats.record_attempt(True)
                    if use_circuit_breaker:
                        _update_circuit_state(func_name, True, circuit_failure_threshold, circuit_recovery_timeout)
                    return result
                except exceptions as e:
                    last_exc = e
                    _retry_stats.record_attempt(False)
                    if on_retry:
                        on_retry(e, i + 1)
                    if condition is not None and not condition(e):
                        break
                    if i == attempts - 1:
                        break
                    wait = base_delay * (factor ** i)
                    if use_jitter:
                        wait = wait * (0.5 + random.random())
                    await asyncio.sleep(wait)
            
            if use_circuit_breaker:
                _update_circuit_state(func_name, False, circuit_failure_threshold, circuit_recovery_timeout)
            raise last_exc
        return wrapper
    return decorator


def get_retry_stats() -> Dict:
    """获取全局重试统计"""
    return _retry_stats.get_stats()


# ==================== H. 线程池/进程池封装 ====================
class PoolExecutor:
    def __init__(self, pool_type="thread", max_workers=None, queue_size=None, on_error="log"):
        self.pool_type = pool_type
        self.max_workers = max_workers or global_config.get("utils.pool.default_max_workers", 8)
        self.queue_size = queue_size or global_config.get("utils.pool.queue_size", 100)
        self.on_error = on_error
        self._executor = None
        self._active_tasks = 0
        self._completed_tasks = 0
        self._lock = threading.Lock()
        self._shutdown = False
        self._queue = queue.Queue(maxsize=self.queue_size)
        
        if pool_type == "thread":
            import concurrent.futures
            self._executor = concurrent.futures.ThreadPoolExecutor(max_workers=self.max_workers)
        elif pool_type == "process":
            import concurrent.futures
            self._executor = concurrent.futures.ProcessPoolExecutor(max_workers=self.max_workers)
        else:
            raise ValueError("pool_type must be 'thread' or 'process'")
    
    def submit(self, fn, *args, **kwargs):
        if self._shutdown:
            raise RuntimeError("Pool has been shutdown")
        future = self._executor.submit(fn, *args, **kwargs)
        with self._lock:
            self._active_tasks += 1
        
        def done_cb(fut):
            with self._lock:
                self._active_tasks -= 1
                self._completed_tasks += 1
            try:
                fut.result()
            except Exception as e:
                if self.on_error == "log":
                    log_error(f"任务执行失败: {e}")
                elif self.on_error == "raise":
                    raise
                elif self.on_error == "ignore":
                    pass
        future.add_done_callback(done_cb)
        return future
    
    def map(self, fn, *iterables):
        return self._executor.map(fn, *iterables)
    
    def shutdown(self, wait=True, timeout=None):
        self._shutdown = True
        self._executor.shutdown(wait=wait, cancel_futures=True)
        if wait and timeout:
            start = time.time()
            while self._active_tasks > 0 and (time.time() - start) < timeout:
                time.sleep(0.1)
    
    def get_active_count(self):
        return self._active_tasks
    
    def get_completed_count(self):
        return self._completed_tasks
    
    def get_queue_size(self):
        return self._queue.qsize()
    
    def __enter__(self):
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        self.shutdown(wait=True)