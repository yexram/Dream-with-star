# src/tools/monitoring.py
"""
系统与网络监控模块
依赖 utils.py 中的部分工具，并利用配置管理器和日志系统
"""

import os
import time
import threading
import sqlite3
import json
import socket
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Callable, Any
from collections import deque
from dataclasses import dataclass
import asyncio

# 第三方依赖
try:
    import psutil
    PSUTIL_AVAILABLE = True
except ImportError:
    PSUTIL_AVAILABLE = False

try:
    import requests
    REQUESTS_AVAILABLE = True
except ImportError:
    REQUESTS_AVAILABLE = False

# 内部依赖
from src.tools.logger import log_info, log_warning, log_error, log_debug
from src.tools.config_manager import config as global_config
from src.tools.utils import NetworkChecker, retry


# ==================== A. 系统信息 ====================
class SystemInfo:
    """系统资源监控（基于 psutil）"""
    
    def __init__(self):
        if not PSUTIL_AVAILABLE:
            raise ImportError("psutil 库未安装，无法使用系统监控")
        self._history = deque(maxlen=global_config.get("monitoring.system.history_size", 2880))
        self._lock = threading.Lock()
        self._alert_callbacks = []
        # 从配置读取告警阈值
        self._cpu_warn = global_config.get("monitoring.system.cpu_warning_threshold", 75)
        self._cpu_crit = global_config.get("monitoring.system.cpu_critical_threshold", 90)
        self._mem_warn = global_config.get("monitoring.system.memory_warning_threshold", 85)
        self._mem_crit = global_config.get("monitoring.system.memory_critical_threshold", 95)
        self._disk_warn = global_config.get("monitoring.system.disk_warning_threshold", 85)
        self._disk_crit = global_config.get("monitoring.system.disk_critical_threshold", 95)
        self._enable_swap = global_config.get("monitoring.system.enable_swap", True)
    
    def register_alert(self, callback: Callable[[str, str, float], None]):
        """注册告警回调 (指标名, 级别, 当前值)"""
        self._alert_callbacks.append(callback)
    
    def collect(self) -> Dict:
        """采集一次当前系统指标"""
        data = {
            "timestamp": time.time(),
            "cpu_percent": psutil.cpu_percent(interval=0.5),
            "cpu_per_core": psutil.cpu_percent(percpu=True),
            "cpu_count": psutil.cpu_count(),
            "memory": psutil.virtual_memory()._asdict(),
            "disk": {},
            "network": psutil.net_io_counters()._asdict(),
            "process": {
                "cpu_percent": psutil.Process().cpu_percent(),
                "memory_percent": psutil.Process().memory_percent(),
                "num_threads": psutil.Process().num_threads(),
                "num_handles": len(psutil.Process().open_files()) if hasattr(psutil.Process(), 'open_files') else 0
            }
        }
        # 安全获取 swap 信息
        if self._enable_swap:
            try:
                data["swap"] = psutil.swap_memory()._asdict()
            except RuntimeError as e:
                log_warning(f"获取 swap 信息失败: {e}，使用空值")
                data["swap"] = {"total": 0, "used": 0, "free": 0, "percent": 0, "sin": 0, "sout": 0}
        else:
            data["swap"] = {"total": 0, "used": 0, "free": 0, "percent": 0, "sin": 0, "sout": 0}
        
        # 磁盘使用率
        for part in psutil.disk_partitions():
            try:
                usage = psutil.disk_usage(part.mountpoint)
                data["disk"][part.mountpoint] = {
                    "total": usage.total,
                    "used": usage.used,
                    "free": usage.free,
                    "percent": usage.percent
                }
            except PermissionError:
                continue
        
        # 告警检查
        for mount, info in data["disk"].items():
            if info["percent"] >= self._disk_crit:
                self._trigger_alert(f"disk.{mount}", "critical", info["percent"])
            elif info["percent"] >= self._disk_warn:
                self._trigger_alert(f"disk.{mount}", "warning", info["percent"])
        
        cpu = data["cpu_percent"]
        if cpu >= self._cpu_crit:
            self._trigger_alert("cpu", "critical", cpu)
        elif cpu >= self._cpu_warn:
            self._trigger_alert("cpu", "warning", cpu)
        
        mem = data["memory"]["percent"]
        if mem >= self._mem_crit:
            self._trigger_alert("memory", "critical", mem)
        elif mem >= self._mem_warn:
            self._trigger_alert("memory", "warning", mem)
        
        with self._lock:
            self._history.append(data)
        return data
    
    def _trigger_alert(self, metric: str, level: str, value: float):
        for cb in self._alert_callbacks:
            try:
                cb(metric, level, value)
            except Exception as e:
                log_error(f"告警回调失败: {e}")
    
    def get_history(self, hours: int = 24) -> List[Dict]:
        """获取最近 hours 小时的历史数据"""
        now = time.time()
        cutoff = now - hours * 3600
        with self._lock:
            return [d for d in self._history if d["timestamp"] >= cutoff]
    
    def get_trend(self, metric: str, hours: int = 1) -> Dict:
        """获取指标变化趋势（环比）"""
        history = self.get_history(hours)
        if len(history) < 2:
            return {}
        first = history[0][metric] if metric in history[0] else 0
        last = history[-1][metric] if metric in history[-1] else 0
        change = last - first
        percent = (change / first * 100) if first != 0 else 0
        return {"start": first, "end": last, "change": change, "percent_change": percent}
    
    def predict_disk_full(self, mount: str) -> Optional[float]:
        """预测磁盘满时间（小时）"""
        history = self.get_history(24)
        if len(history) < 6:
            return None
        timestamps = [d["timestamp"] for d in history]
        usages = [d["disk"].get(mount, {}).get("percent", 0) for d in history]
        if len(usages) < 2:
            return None
        try:
            import numpy as np
            x = np.array(timestamps)
            y = np.array(usages)
            slope, _ = np.polyfit(x, y, 1)
            if slope <= 0:
                return None
            remaining = (100 - y[-1]) / slope
            return remaining / 3600
        except:
            return None


# ==================== B. 网络探测（后台监控）====================
@dataclass
class ProbeTarget:
    host: str
    probe_type: str  # icmp, tcp, http
    port: int = 0
    timeout: float = 3
    interval: int = 60


class NetworkProbeMonitor:
    """长期后台网络探测，结果持久化到 SQLite"""
    
    def __init__(self, db_path: str = None):
        self.db_path = db_path or global_config.get("monitoring.network_probe.persist_db", "data/monitoring.db")
        self._targets: List[ProbeTarget] = []
        self._stop_event = threading.Event()
        self._thread = None
        self._failure_counts = {}
        self._alert_callback = None
        self._init_db()
    
    def _init_db(self):
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS probe_results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp REAL,
                target TEXT,
                probe_type TEXT,
                success INTEGER,
                rtt REAL,
                error TEXT
            )
        """)
        self._conn.commit()
    
    def add_target(self, target: ProbeTarget):
        self._targets.append(target)
        self._failure_counts[target.host] = 0
    
    def set_alert_callback(self, callback: Callable[[str, bool, int], None]):
        """告警回调：target, is_failure, consecutive_failures"""
        self._alert_callback = callback
    
    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        log_info("网络探测监控已启动")
    
    def stop(self):
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)
        self._conn.close()
        log_info("网络探测监控已停止")
    
    def _run(self):
        while not self._stop_event.is_set():
            for target in self._targets:
                self._probe_one(target)
            interval = global_config.get("monitoring.network_probe.interval", 60)
            self._stop_event.wait(interval)
    
    def _probe_one(self, target: ProbeTarget):
        start = time.time()
        success = False
        rtt = None
        error = None
        try:
            if target.probe_type == "icmp":
                success, rtt = NetworkChecker.ping(target.host, timeout=target.timeout)
            elif target.probe_type == "tcp":
                success = NetworkChecker.check_port(target.host, target.port, target.timeout)
                if success:
                    rtt = 0
            elif target.probe_type == "http":
                if REQUESTS_AVAILABLE:
                    rtt = NetworkChecker.measure_rtt_http(target.host, target.timeout)
                    success = rtt is not None
                else:
                    success = False
                    error = "requests not installed"
            else:
                error = "unsupported type"
        except Exception as e:
            error = str(e)
        
        elapsed = time.time() - start
        self._store_result(target, success, rtt if success else None, error)
        
        threshold = global_config.get("monitoring.network_probe.failure_threshold", 3)
        if not success:
            self._failure_counts[target.host] = self._failure_counts.get(target.host, 0) + 1
            if self._failure_counts[target.host] == threshold and self._alert_callback:
                self._alert_callback(target.host, True, threshold)
        else:
            if self._failure_counts.get(target.host, 0) >= threshold and self._alert_callback:
                self._alert_callback(target.host, False, 0)
            self._failure_counts[target.host] = 0
    
    def _store_result(self, target: ProbeTarget, success: bool, rtt: Optional[float], error: Optional[str]):
        self._conn.execute(
            "INSERT INTO probe_results (timestamp, target, probe_type, success, rtt, error) VALUES (?, ?, ?, ?, ?, ?)",
            (time.time(), target.host, target.probe_type, 1 if success else 0, rtt, error)
        )
        self._conn.commit()
    
    def get_report(self, hours: int = 24) -> Dict:
        """生成健康报告"""
        cutoff = time.time() - hours * 3600
        results = self._conn.execute(
            "SELECT target, success, rtt FROM probe_results WHERE timestamp > ?",
            (cutoff,)
        ).fetchall()
        if not results:
            return {}
        stats = {}
        for target, success, rtt in results:
            if target not in stats:
                stats[target] = {"total": 0, "success": 0, "rtts": []}
            stats[target]["total"] += 1
            if success:
                stats[target]["success"] += 1
                if rtt:
                    stats[target]["rtts"].append(rtt)
        report = {}
        for target, data in stats.items():
            availability = data["success"] / data["total"] * 100
            rtts = data["rtts"]
            avg_rtt = sum(rtts) / len(rtts) if rtts else None
            report[target] = {
                "availability_percent": round(availability, 2),
                "total_probes": data["total"],
                "failed_probes": data["total"] - data["success"],
                "avg_rtt_ms": round(avg_rtt * 1000, 2) if avg_rtt else None
            }
        return report


# ==================== C. RTT 测量增强 ====================
class RTTProbe:
    """延迟测量增强，支持 ICMP, TCP, HTTP, DNS"""
    
    @staticmethod
    def measure(target: str, probe_type: str, timeout: float = 3, port: int = 80) -> Optional[float]:
        if probe_type == "icmp":
            _, rtt = NetworkChecker.ping(target, timeout)
            return rtt
        elif probe_type == "tcp":
            start = time.time()
            try:
                socket.create_connection((target, port), timeout=timeout)
                return time.time() - start
            except:
                return None
        elif probe_type == "http":
            return NetworkChecker.measure_rtt_http(target, timeout)
        elif probe_type == "dns":
            try:
                start = time.time()
                socket.gethostbyname(target)
                return time.time() - start
            except:
                return None
        else:
            return None
    
    @staticmethod
    def measure_batch(targets: List[Dict], concurrency: int = 5) -> List[Dict]:
        """批量并发测量"""
        import concurrent.futures
        results = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as executor:
            futures = []
            for t in targets:
                future = executor.submit(
                    RTTProbe.measure,
                    t["host"], t["type"], t.get("timeout", 3), t.get("port", 80)
                )
                futures.append((t, future))
            for t, future in futures:
                rtt = future.result()
                results.append({"host": t["host"], "rtt": rtt})
        return results
    
    @staticmethod
    def stats(rtts: List[float], ddof: int = None) -> Dict:
        """计算统计量，ddof 为样本标准差自由度，默认从配置读取（0=总体标准差）"""
        if not rtts:
            return {}
        import numpy as np
        if ddof is None:
            ddof = global_config.get("monitoring.rtt.stats_ddof", 0)
        return {
            "min": min(rtts),
            "max": max(rtts),
            "avg": sum(rtts) / len(rtts),
            "std": np.std(rtts, ddof=ddof),
            "median": np.median(rtts),
            "count": len(rtts)
        }