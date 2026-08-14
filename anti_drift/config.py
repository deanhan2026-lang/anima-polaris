"""Polaris 配置加载
优先级：环境变量 > 模块 config.yaml > 根 config.yaml > 默认值
"""
import yaml
import os
from pathlib import Path


def _load_yaml(path: Path) -> dict:
    if path.exists():
        try:
            with open(path, encoding='utf-8') as f:
                return yaml.safe_load(f) or {}
        except Exception:
            pass
    return {}


_repo_root = Path(__file__).parent.parent
_root_cfg = _load_yaml(_repo_root / 'config.yaml')                 # 根统一配置
_mod_cfg  = _load_yaml(Path(__file__).parent / 'config.yaml')        # 模块私有配置


def get(key: str, default=None):
    """
    按点号路径读取配置值
    示例:
        get('detector.weights.semantic')  → 0.40
        get('server.port')               → 5051
        get('nonexistent.key', 'fallback') → 'fallback'
    """
    # 1. 环境变量（最高优先级，格式 POLARIS_KEY_SUBKEY=value）
    env_key = 'POLARIS_' + key.upper().replace('.', '_')
    env_val = os.environ.get(env_key)
    if env_val is not None:
        return env_val

    # 2. 模块私有配置（优先级高于根配置）
    keys = key.split('.')
    val = _mod_cfg
    for k in keys:
        if isinstance(val, dict):
            val = val.get(k, None)
            if val is None:
                break
        else:
            val = None
            break
    if val is not None:
        return val

    # 3. 根统一配置回退
    val = _root_cfg
    for k in keys:
        if isinstance(val, dict):
            val = val.get(k, None)
            if val is None:
                break
        else:
            val = None
            break
    return val if val is not None else default
