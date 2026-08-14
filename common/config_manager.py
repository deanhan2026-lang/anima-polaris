#!/usr/bin/env python3
"""
硅基文明知识库 — 中心化配置管理
支持从 YAML 文件加载配置，支持热加载（可选）
"""
import yaml
import json
from pathlib import Path
from typing import Any, Dict, Optional
from threading import RLock


class ConfigManager:
    """中心化配置管理器（单例模式）"""

    _instance = None
    _lock = RLock()

    def __new__(cls, *args, **kwargs):
        if not cls._instance:
            with cls._lock:
                if not cls._instance:
                    cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self, config_file: Optional[str] = None):
        """
        初始化配置管理器

        Args:
            config_file: 配置文件路径（YAML 或 JSON）
        """
        self._config_file = config_file or "config.yaml"
        self._config = {}
        self._last_mtime = 0
        self._lock = RLock()
        self.load()

    def load(self, config_file: Optional[str] = None) -> bool:
        """
        加载配置文件

        Args:
            config_file: 配置文件路径（可选，默认使用初始化时的路径）

        Returns:
            bool: 是否加载成功
        """
        with self._lock:
            target_file = config_file or self._config_file
            path = Path(target_file)

            if not path.exists():
                # 配置文件不存在，使用默认配置
                self._config = self._default_config()
                return False

            try:
                with open(path, "r", encoding="utf-8") as f:
                    if path.suffix in [".yaml", ".yml"]:
                        self._config = yaml.safe_load(f) or {}
                    elif path.suffix == ".json":
                        self._config = json.load(f) or {}
                    else:
                        raise ValueError(f"Unsupported config format: {path.suffix}")

                self._config_file = target_file
                self._last_mtime = path.stat().st_mtime
                return True

            except Exception as e:
                # 加载失败，使用默认配置
                self._config = self._default_config()
                return False

    def reload_if_changed(self) -> bool:
        """
        如果配置文件已修改，重新加载

        Returns:
            bool: 是否重新加载了配置
        """
        with self._lock:
            path = Path(self._config_file)
            if not path.exists():
                return False

            current_mtime = path.stat().st_mtime
            if current_mtime > self._last_mtime:
                self.load()
                return True
            return False

    def get(self, key: str, default: Any = None) -> Any:
        """
        获取配置值（支持点号路径）

        Args:
            key: 配置键（如 "database.host"）
            default: 默认值

        Returns:
            Any: 配置值
        """
        with self._lock:
            keys = key.split(".")
            value = self._config

            for k in keys:
                if isinstance(value, dict) and k in value:
                    value = value[k]
                else:
                    return default

            return value

    def set(self, key: str, value: Any) -> None:
        """
        设置配置值（支持点号路径）

        Args:
            key: 配置键（如 "database.host"）
            value: 配置值
        """
        with self._lock:
            keys = key.split(".")
            config = self._config

            for k in keys[:-1]:
                if k not in config or not isinstance(config[k], dict):
                    config[k] = {}
                config = config[k]

            config[keys[-1]] = value

    def save(self, config_file: Optional[str] = None) -> bool:
        """
        保存配置到文件

        Args:
            config_file: 配置文件路径（可选）

        Returns:
            bool: 是否保存成功
        """
        with self._lock:
            target_file = config_file or self._config_file
            path = Path(target_file)

            try:
                with open(path, "w", encoding="utf-8") as f:
                    if path.suffix in [".yaml", ".yml"]:
                        yaml.dump(self._config, f, allow_unicode=True, default_flow_style=False)
                    elif path.suffix == ".json":
                        json.dump(self._config, f, ensure_ascii=False, indent=2)
                    else:
                        raise ValueError(f"Unsupported config format: {path.suffix}")

                self._last_mtime = path.stat().st_mtime
                return True

            except Exception as e:
                return False

    def _default_config(self) -> Dict[str, Any]:
        """默认配置"""
        return {
            "logging": {
                "level": "INFO",
                "file": "logs/silicon-civilization-kb.log",
                "max_bytes": 10 * 1024 * 1024,  # 10MB
                "backup_count": 5,
            },
            "database": {
                "url": "sqlite:///silicon-civilization.db",
                "echo": False,
            },
            "kb": {
                "dir": "knowledge-base",
                "hash_index": "knowledge-base/.hash_index.json",
                "auto_save": True,
            },
            "memguard": {
                "enabled": True,
                "keys_dir": "keys",
                "audit_log": "logs/memguard-audit.jsonl",
            },
            "polaris": {
                "enabled": True,
                "port": 5052,
                "sampling_interval": 300,  # 5 minutes
            },
        }


# 全局配置管理器实例
_config_manager = None

def get_config_manager(config_file: Optional[str] = None) -> ConfigManager:
    """获取全局配置管理器实例"""
    global _config_manager
    if _config_manager is None:
        _config_manager = ConfigManager(config_file)
    return _config_manager


def get_config(key: str, default: Any = None) -> Any:
    """快捷方式：获取配置值"""
    return get_config_manager().get(key, default)


def set_config(key: str, value: Any) -> None:
    """快捷方式：设置配置值"""
    get_config_manager().set(key, value)


def reload_config() -> bool:
    """快捷方式：重新加载配置"""
    return get_config_manager().reload_if_changed()
