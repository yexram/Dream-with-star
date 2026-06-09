"""
全项目通用配置管理模块
功能：
- 支持 JSON / YAML 配置文件（默认 JSON）
- 支持环境变量覆盖（前缀 APP_）
- 支持配置热重载（文件监控）
- 支持配置变更回调（传递旧值和新值）
- 线程安全
- 链式加载：默认值 < 文件 < 环境变量
"""

import json
import os
import threading
import copy
from pathlib import Path
from typing import Any, Dict, Optional, Callable, List
from datetime import datetime
import sys

try:
    import yaml
    YAML_AVAILABLE = True
except ImportError:
    YAML_AVAILABLE = False


class ConfigManager:
    """配置管理器单例"""
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
        self._config: Dict[str, Any] = {}
        self._defaults: Dict[str, Any] = {}
        self._watchers: List[Callable[[str, Any, Any], None]] = []
        self._watch_thread: Optional[threading.Thread] = None
        self._stop_watch = threading.Event()
        self._watch_interval = 1.0  # 秒
        self._config_path: Optional[Path] = None
        self._file_mtime: Optional[float] = None
        # 存储配置快照，用于回调时传递旧值
        self._config_snapshot: Dict[str, Any] = {}
        self._lock = threading.RLock()
        self._initialized = True

    def set_defaults(self, defaults: Dict[str, Any]):
        """设置默认配置（优先级最低）"""
        with self._lock:
            self._defaults = copy.deepcopy(defaults)
            self._merge_defaults()
            self._take_snapshot()

    def _merge_defaults(self):
        """将默认配置合并到当前配置（不覆盖已有值）"""
        for key, value in self._defaults.items():
            if key not in self._config:
                self._config[key] = value

    def _take_snapshot(self):
        """保存当前配置的快照"""
        with self._lock:
            self._config_snapshot = copy.deepcopy(self._config)

    def load_file(self, file_path: str, format: str = "auto"):
        """
        从文件加载配置，支持 JSON / YAML
        :param file_path: 配置文件路径
        :param format: auto / json / yaml
        """
        path = Path(file_path)
        if not path.exists():
            raise FileNotFoundError(f"配置文件不存在: {file_path}")

        if format == "auto":
            suffix = path.suffix.lower()
            if suffix in [".yaml", ".yml"]:
                format = "yaml"
            elif suffix == ".json":
                format = "json"
            else:
                raise ValueError(f"不支持的文件格式: {suffix}")

        with open(path, 'r', encoding='utf-8') as f:
            if format == "json":
                new_config = json.load(f)
            elif format == "yaml":
                if not YAML_AVAILABLE:
                    raise ImportError("需要安装 PyYAML: pip install pyyaml")
                new_config = yaml.safe_load(f)
            else:
                raise ValueError(f"未知格式: {format}")
            
        if new_config is None:
            new_config = {}

        with self._lock:
            # 保存旧快照
            old_snapshot = copy.deepcopy(self._config)
            # 更新配置（保留默认值逻辑）
            self._config = new_config
            self._merge_defaults()
            self._config_path = path
            self._file_mtime = path.stat().st_mtime
            # 通知变更
            changed_keys = self._diff_config(old_snapshot, self._config)
            for key in changed_keys:
                old_val = self._get_nested(old_snapshot, key)
                new_val = self._get_nested(self._config, key)
                self._notify_watchers(key, old_val, new_val)
            self._take_snapshot()

    def load_chain(self, defaults: Dict[str, Any], file_path: str, env_prefix: str = "APP_"):
        """
        链式加载：先设置默认值，再加载配置文件（覆盖），最后加载环境变量（最高优先级）
        """
        self.set_defaults(defaults)
        self.load_file(file_path)
        self.load_from_env(env_prefix)

    def load_from_env(self, prefix: str = "APP_"):
        """
        从环境变量加载配置，支持点号路径，例如 APP_LOG_LEVEL=DEBUG 映射到 log.level
        规则：将环境变量名转换为小写，去掉前缀，下划线替换为点号
        """
        with self._lock:
            old_snapshot = copy.deepcopy(self._config)
            for env_key, env_value in os.environ.items():
                if env_key.startswith(prefix):
                    key_path = env_key[len(prefix):].lower().replace('_', '.')
                    parsed_value = self._parse_env_value(env_value)
                    self._set_nested(key_path, parsed_value)
            # 通知变更
            changed_keys = self._diff_config(old_snapshot, self._config)
            for key in changed_keys:
                old_val = self._get_nested(old_snapshot, key)
                new_val = self._get_nested(self._config, key)
                self._notify_watchers(key, old_val, new_val)
            self._take_snapshot()

    @staticmethod
    def _parse_env_value(value: str) -> Any:
        """尝试将环境变量值转换为正确类型"""
        if value.lower() == 'true':
            return True
        if value.lower() == 'false':
            return False
        if value.isdigit():
            return int(value)
        try:
            return float(value)
        except ValueError:
            return value

    def _set_nested(self, key_path: str, value: Any):
        """根据点号路径设置嵌套字典"""
        parts = key_path.split('.')
        target = self._config
        for part in parts[:-1]:
            if part not in target:
                target[part] = {}
            target = target[part]
        target[parts[-1]] = value

    def _get_nested(self, config: Dict, key_path: str, default=None) -> Any:
        """根据点号路径获取嵌套值"""
        parts = key_path.split('.')
        target = config
        try:
            for part in parts:
                target = target[part]
            return target
        except (KeyError, TypeError):
            return default

    def get(self, key_path: str, default=None) -> Any:
        """获取配置值，支持点号路径"""
        with self._lock:
            return self._get_nested(self._config, key_path, default)

    def set(self, key_path: str, value: Any):
        """动态设置配置值（不持久化）"""
        with self._lock:
            old_snapshot = copy.deepcopy(self._config)
            self._set_nested(key_path, value)
            # 通知变更
            old_val = self._get_nested(old_snapshot, key_path)
            self._notify_watchers(key_path, old_val, value)
            self._take_snapshot()

    def reload(self):
        """热重载配置文件"""
        if self._config_path is None:
            raise RuntimeError("未加载过配置文件，无法重载")
        if not self._config_path.exists():
            raise FileNotFoundError(f"配置文件已消失: {self._config_path}")
        new_mtime = self._config_path.stat().st_mtime
        if new_mtime == self._file_mtime:
            return False  # 未变化
        # 重新加载
        old_config = self._config.copy()
        self.load_file(str(self._config_path))
        return True

    def _diff_config(self, old: Dict, new: Dict, prefix: str = "") -> List[str]:
        """递归比较配置差异，返回变更的键路径列表"""
        changed = []
        all_keys = set(old.keys()) | set(new.keys())
        for key in all_keys:
            full_key = f"{prefix}.{key}" if prefix else key
            old_val = old.get(key)
            new_val = new.get(key)
            if old_val != new_val:
                changed.append(full_key)
            elif isinstance(old_val, dict) and isinstance(new_val, dict):
                changed.extend(self._diff_config(old_val, new_val, full_key))
        return changed

    def _notify_watchers(self, key_path: str, old_value: Any, new_value: Any):
        """通知所有监听器，传递旧值和新值"""
        for callback in self._watchers:
            try:
                callback(key_path, old_value, new_value)
            except Exception as e:
                # 使用 sys.stderr 避免循环依赖
                print(f"配置变更回调执行失败: {e}", file=sys.stderr)

    def watch(self, callback: Callable[[str, Any, Any], None]):
        """注册配置变更监听器"""
        with self._lock:
            self._watchers.append(callback)

    def start_watch_thread(self, interval: float = 1.0):
        """启动后台线程监控配置文件变化"""
        if self._watch_thread and self._watch_thread.is_alive():
            return
        self._watch_interval = interval
        self._stop_watch.clear()
        self._watch_thread = threading.Thread(target=self._watch_loop, daemon=True)
        self._watch_thread.start()

    def stop_watch_thread(self):
        """停止监控线程"""
        self._stop_watch.set()
        if self._watch_thread:
            self._watch_thread.join(timeout=2)

    def _watch_loop(self):
        while not self._stop_watch.wait(self._watch_interval):
            try:
                self.reload()
            except Exception as e:
                print(f"配置热重载失败: {e}", file=sys.stderr)

    def get_all(self) -> Dict:
        """获取完整配置（深拷贝）"""
        with self._lock:
            return copy.deepcopy(self._config)


# 全局单例
config = ConfigManager()