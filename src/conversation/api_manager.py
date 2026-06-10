# src/conversation/api_manager.py
"""
API密钥管理器（完整版）
- 支持多模块、多密钥、加密存储
- 支持加权轮询、故障转移策略
- 支持配置热重载（监听 api_keys.json 变化）
"""

import json
import os
from pathlib import Path
from threading import Lock
from typing import Dict, List, Optional, Any
from cryptography.fernet import Fernet
from src.tools.config_manager import config
from src.tools.logger import log_info, log_warning, log_error, log_debug

# ==================== 加密管理器 ====================
class EncryptionManager:
    def __init__(self, encryption_key: bytes):
        self.cipher = Fernet(encryption_key)

    @classmethod
    def generate_key(cls) -> bytes:
        return Fernet.generate_key()

    def encrypt(self, plain_text: str) -> str:
        return self.cipher.encrypt(plain_text.encode()).decode()

    def decrypt(self, encrypted_text: str) -> str:
        return self.cipher.decrypt(encrypted_text.encode()).decode()


# ==================== 主密钥管理 ====================
class MasterKeyManager:
    @staticmethod
    def get_master_key() -> bytes:
        key_path = Path(config.get("encryption.master_key_file", "data/secrets/master.key"))
        key_path.parent.mkdir(parents=True, exist_ok=True)
        if key_path.exists():
            with open(key_path, "rb") as f:
                return f.read()
        else:
            master_key = EncryptionManager.generate_key()
            with open(key_path, "wb") as f:
                f.write(master_key)
            log_info("已生成新的主密钥", path=str(key_path))
            return master_key


# ==================== 密钥选择器 ====================
class WeightedRoundRobinSelector:
    def __init__(self, keys: List[Dict]):
        self.keys = [k for k in keys if k.get("enabled", True)]
        self.weights = [k.get("weight", 1) for k in self.keys]
        self.current = -1
        self.max_weight = max(self.weights) if self.weights else 0
        self.gcd_val = self._gcd_list(self.weights)
        self.current_weight = self.max_weight   # 修复：初始化 current_weight

    def _gcd_list(self, nums):
        from math import gcd
        g = nums[0] if nums else 1
        for n in nums[1:]:
            g = gcd(g, n)
        return g

    def next_key(self) -> Optional[str]:
        if not self.keys:
            return None
        while True:
            self.current = (self.current + 1) % len(self.keys)
            if self.current == 0:
                self.current_weight = self.current_weight - self.gcd_val if hasattr(self, 'current_weight') else self.max_weight
                if self.current_weight <= 0:
                    self.current_weight = self.max_weight
                    if self.current_weight == 0:
                        return None
            if self.weights[self.current] >= self.current_weight:
                return self.keys[self.current]["plain"]


class FailoverSelector:
    def __init__(self, keys: List[Dict]):
        self.keys = [k for k in keys if k.get("enabled", True)]
        self.index = 0

    def next_key(self) -> Optional[str]:
        if not self.keys:
            return None
        key = self.keys[self.index % len(self.keys)]
        self.index += 1
        return key["plain"]


# ==================== 核心密钥管理器（单例 + 热重载）====================
class KeyManager:
    _instance = None
    _lock = Lock()

    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        if hasattr(self, '_initialized'):
            return
        self._initialized = True
        self._lock = Lock()
        self.crypto = EncryptionManager(MasterKeyManager.get_master_key())
        self.api_keys_file = Path(config.get("api_keys_file", "data/secrets/api_keys.json"))
        self.modules: Dict[int, Dict] = {}
        self._load_from_file()
        self._register_hot_reload()

    def _register_hot_reload(self):
        """注册配置文件热重载回调"""
        def on_config_change(key_path, old, new):
            if key_path == "api_keys_file":
                self.api_keys_file = Path(new)
                self.reload()
        config.watch(on_config_change)

    def reload(self):
        """手动重新加载密钥配置"""
        with self._lock:
            log_info("重新加载密钥配置")
            self._load_from_file()

    def _load_from_file(self):
        if not self.api_keys_file.exists():
            self._save_to_file()
        try:
            with open(self.api_keys_file, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            log_error("加载密钥文件失败", error=str(e))
            return

        new_modules = {}
        for module_code_str, module_cfg in data.get("modules", {}).items():
            module_id = int(module_code_str)
            strategy = module_cfg.get("strategy", "weighted_round_robin")
            keys_encrypted = module_cfg.get("keys", [])
            decrypted_keys = []
            for k in keys_encrypted:
                try:
                    plain = self.crypto.decrypt(k["encrypted"])
                except Exception as e:
                    log_error("解密密钥失败", key_id=k.get("id"), error=str(e))
                    continue
                decrypted_keys.append({
                    "id": k.get("id"),
                    "plain": plain,
                    "weight": k.get("weight", 1),
                    "enabled": k.get("enabled", True),
                    "failures": 0
                })
            selector = None
            if strategy == "weighted_round_robin":
                selector = WeightedRoundRobinSelector(decrypted_keys)
            elif strategy == "failover":
                selector = FailoverSelector(decrypted_keys)
            else:
                selector = WeightedRoundRobinSelector(decrypted_keys)

            new_modules[module_id] = {
                "strategy": strategy,
                "selector": selector,
                "keys": decrypted_keys,
                "config": module_cfg
            }
        self.modules = new_modules
        log_info("密钥配置加载完成", modules=list(self.modules.keys()))

    def _save_to_file(self):
        """将当前内存配置保存到文件（加密）"""
        data = {"modules": {}}
        for module_id, mod in self.modules.items():
            keys_list = []
            for k in mod["keys"]:
                encrypted = k.get("encrypted")
                if "plain" in k and not encrypted:
                    encrypted = self.crypto.encrypt(k["plain"])
                keys_list.append({
                    "id": k.get("id"),
                    "encrypted": encrypted,
                    "weight": k.get("weight", 1),
                    "enabled": k.get("enabled", True)
                })
            data["modules"][str(module_id)] = {
                "strategy": mod["strategy"],
                "keys": keys_list
            }
        self.api_keys_file.parent.mkdir(parents=True, exist_ok=True)
        with open(self.api_keys_file, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        log_info("密钥配置已保存")

    def get_next_key(self, module_id: int) -> Optional[str]:
        with self._lock:
            mod = self.modules.get(module_id)
            if not mod or not mod["selector"]:
                log_warning("模块未注册或无可用密钥", module_id=module_id)
                return None
            return mod["selector"].next_key()

    def mark_key_failure(self, module_id: int, key_plain: str):
        with self._lock:
            mod = self.modules.get(module_id)
            if not mod:
                return
            for k in mod["keys"]:
                if k["plain"] == key_plain:
                    k["failures"] = k.get("failures", 0) + 1
                    if k["failures"] >= 3:
                        k["enabled"] = False
                        log_warning("密钥连续失败3次，已禁用", module_id=module_id, key_id=k.get("id"))
                    self._rebuild_selector(module_id)
                    self._save_to_file()
                    break

    def register_module(self, module_id: int, strategy: str = "weighted_round_robin"):
        with self._lock:
            if module_id in self.modules:
                log_warning("模块已存在，将更新策略", module_id=module_id)
            self.modules[module_id] = {
                "strategy": strategy,
                "selector": None,
                "keys": [],
                "config": {"strategy": strategy}
            }
            self._rebuild_selector(module_id)
            self._save_to_file()
            log_info("已注册模块", module_id=module_id)

    def add_key(self, module_id: int, api_key_plain: str, key_id: str = None, weight: int = 1):
        with self._lock:
            if module_id not in self.modules:
                raise ValueError(f"模块 {module_id} 未注册")
            if key_id is None:
                key_id = f"key_{len(self.modules[module_id]['keys']) + 1}"
            encrypted = self.crypto.encrypt(api_key_plain)
            new_key = {
                "id": key_id,
                "plain": api_key_plain,
                "encrypted": encrypted,
                "weight": weight,
                "enabled": True,
                "failures": 0
            }
            self.modules[module_id]["keys"].append(new_key)
            self._rebuild_selector(module_id)
            self._save_to_file()
            log_info("已添加密钥", module_id=module_id, key_id=key_id)

    def _rebuild_selector(self, module_id: int):
        mod = self.modules.get(module_id)
        if not mod:
            return
        strategy = mod["strategy"]
        keys = mod["keys"]
        if strategy == "weighted_round_robin":
            mod["selector"] = WeightedRoundRobinSelector(keys)
        elif strategy == "failover":
            mod["selector"] = FailoverSelector(keys)
        else:
            mod["selector"] = WeightedRoundRobinSelector(keys)


# 全局单例
key_manager = KeyManager()