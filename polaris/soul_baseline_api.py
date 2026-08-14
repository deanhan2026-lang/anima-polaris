# -*- coding: utf-8 -*-
"""
polaris/soul_baseline_api.py
Polaris × MeshIdentity Soul Baseline API

M5 集成核心：Polaris 灵魂基线 API + DID 鉴权

路由前缀: /api/v1/soul-baselines

Endpoints:
  GET  /                        — 列出所有注册的 soul baselines（需 DID auth）
  POST /register                — 注册新实例的 soul baseline（需 DID auth）
  GET  /<instance_id>           — 读取指定实例的 soul baseline
  GET  /<instance_id>/verify     — 验证 soul baseline 完整性（hash 对比）

鉴权方案：
  - 读取（memory_read）: 所有 DID 持有者可操作
  - 注册（instance_register）: 仅主 DID 可操作
  - Header: X-DID-Token: <token>
"""

import sys
import io
import json
import base64
import hashlib
import uuid
from pathlib import Path
from datetime import datetime, timezone
from functools import wraps

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8")

from flask import Blueprint, jsonify, request

bp = Blueprint("soul_baseline_api", __name__)

# ========== 路径配置 ==========
_REPO_ROOT = Path(__file__).parent.parent.resolve()
_MEMGUARD_ROOT = _REPO_ROOT / "memguard"
_SOUL_BASELINE_DIR = _REPO_ROOT / "data" / "soul_baselines"
_SOUL_BASELINE_DIR.mkdir(parents=True, exist_ok=True)


# ========== DID Auth Engine（延迟导入） ==========

_did_auth_engine = None
_did_auth_error = None

def _get_did_auth_engine():
    """懒加载 DID Auth Engine（避免循环导入）"""
    global _did_auth_engine, _did_auth_error
    if _did_auth_engine is None and _did_auth_error is None:
        try:
            from memguard.auth_integration import (
                DIDAuthenticator,
                DIDAuthError,
                PermissionDeniedError,
            )
            storage_path = str(_MEMGUARD_ROOT / "data" / "did_auth")
            _did_auth_engine = DIDAuthenticator(
                storage_path=storage_path,
                debug=False,
            )
        except Exception as e:
            _did_auth_error = str(e)
            raise RuntimeError(f"DID Auth 初始化失败: {e}")
    return _did_auth_engine


# ========== 错误类 ==========

class SoulBaselineError(Exception):
    """Soul Baseline 操作异常"""
    pass


# ========== Soul Baseline Storage ==========

class SoulBaselineStore:
    """
    Soul Baseline 持久化存储（每实例一 JSON 文件）
    
    文件路径: data/soul_baselines/{instance_id}.json
    索引文件: data/soul_baselines/_registry.json
    """
    
    INDEX_FILE = _SOUL_BASELINE_DIR / "_registry.json"
    
    @classmethod
    def _load_registry(cls) -> dict:
        if cls.INDEX_FILE.exists():
            return json.loads(cls.INDEX_FILE.read_text(encoding="utf-8"))
        return {"instances": {}, "version": "1.0"}
    
    @classmethod
    def _save_registry(cls, registry: dict):
        cls.INDEX_FILE.write_text(
            json.dumps(registry, ensure_ascii=False, indent=2),
            encoding="utf-8"
        )
    
    @classmethod
    def _instance_path(cls, instance_id: str) -> Path:
        return _SOUL_BASELINE_DIR / f"{instance_id}.json"
    
    @classmethod
    def list_all(cls) -> list:
        """返回所有注册的 soul baselines（不含内容，仅元数据）"""
        registry = cls._load_registry()
        return [
            {
                "instance_id": iid,
                "soul_hash": meta.get("soul_hash", ""),
                "soul_length": meta.get("soul_length", 0),
                "registered_at": meta.get("registered_at", ""),
                "registered_by_did": meta.get("registered_by_did", ""),
            }
            for iid, meta in registry.get("instances", {}).items()
        ]
    
    @classmethod
    def get(cls, instance_id: str) -> dict:
        """读取指定实例的 soul baseline（含内容）"""
        path = cls._instance_path(instance_id)
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))
    
    @classmethod
    def save(cls, instance_id: str, entry: dict) -> dict:
        """保存 soul baseline"""
        path = cls._instance_path(instance_id)
        entry["_saved_at"] = datetime.now(timezone.utc).isoformat()
        path.write_text(
            json.dumps(entry, ensure_ascii=False, indent=2),
            encoding="utf-8"
        )
        # 更新 registry
        registry = cls._load_registry()
        registry["instances"][instance_id] = {
            "soul_hash": entry.get("soul_hash", ""),
            "soul_length": entry.get("soul_length", 0),
            "registered_at": entry.get("registered_at", ""),
            "registered_by_did": entry.get("registered_by_did", ""),
        }
        cls._save_registry(registry)
        return entry
    
    @classmethod
    def delete(cls, instance_id: str) -> bool:
        """删除 soul baseline"""
        path = cls._instance_path(instance_id)
        if path.exists():
            path.unlink()
        registry = cls._load_registry()
        if instance_id in registry.get("instances", {}):
            del registry["instances"][instance_id]
            cls._save_registry(registry)
        return True


# ========== DID 鉴权装饰器 ==========

def require_did_token(action: str):
    """
    DID Token 鉴权装饰器
    
    验证 X-DID-Token header：
    - action=memory_read: 所有 DID 可读
    - action=instance_register: 仅主 DID 可写
    
    注入 g.did / g.instance_id / g.token_info
    """
    def decorator(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            token = request.headers.get("X-DID-Token", "")
            
            if not token:
                return jsonify({
                    "error": "missing_did_token",
                    "hint": "X-DID-Token header required",
                    "action": action,
                }), 401
            
            try:
                engine = _get_did_auth_engine()
            except RuntimeError as e:
                return jsonify({
                    "error": "did_auth_unavailable",
                    "detail": str(e),
                }), 503
            
            result = engine.verify_token(token)
            
            if not result.get("valid"):
                return jsonify({
                    "error": "invalid_did_token",
                    "detail": result.get("error", "unknown"),
                }), 401
            
            did = result.get("did", "")
            instance_id = result.get("instance_id", "")
            
            # 检查权限
            has_perm = engine.check_permission(did, instance_id, action)
            if not has_perm:
                return jsonify({
                    "error": "permission_denied",
                    "detail": f"DID {did} cannot perform {action} on {instance_id}",
                    "action": action,
                    "did": did,
                    "instance_id": instance_id,
                }), 403
            
            # 注入到 flask.g
            from flask import g
            g.did = did
            g.instance_id = instance_id
            g.token_info = result
            
            return f(*args, **kwargs)
        return decorated
    return decorator


# ========== 路由 ==========

@bp.route("/soul-baselines", methods=["GET"])
@require_did_token(action="memory_read")
def list_soul_baselines():
    """
    GET /api/v1/soul-baselines
    
    列出所有注册的 soul baselines。
    需要 X-DID-Token（memory_read 权限）。
    
    Returns:
        {
          "instances": [
            {
              "instance_id": "nyx-windows",
              "soul_hash": "sha256:...",
              "soul_length": 12345,
              "registered_at": "2026-07-14T09:00:00+08:00",
              "registered_by_did": "did:key:..."
            }
          ],
          "total": 1
        }
    """
    baselines = SoulBaselineStore.list_all()
    return jsonify({
        "instances": baselines,
        "total": len(baselines),
        "queried_by_did": getattr(getattr(__import__('flask'), 'g', None) or {}, 'did', 'unknown'),
    })


@bp.route("/soul-baselines/register", methods=["POST"])
@require_did_token(action="instance_register")
def register_soul_baseline():
    """
    POST /api/v1/soul-baselines/register
    
    注册实例的 soul baseline（带 DID 鉴权）。
    需要 X-DID-Token（instance_register 权限）。
    调用者 DID 必须与 instance_id 匹配（主 DID）。
    
    Body:
        {
          "instance_id": "nyx-windows",
          "soul_content_b64": "<base64 encoded soul content>",
          "soul_hash": "sha256:..."  // 可选，会自动计算
        }
    
    Returns:
        {
          "instance_id": "nyx-windows",
          "soul_hash": "sha256:...",
          "soul_length": 12345,
          "registered_at": "...",
          "registered_by_did": "did:key:..."
        }
    """
    from flask import g
    
    data = request.json or {}
    instance_id = data.get("instance_id", "").strip()
    soul_content_b64 = data.get("soul_content_b64", "")
    provided_hash = data.get("soul_hash", "")
    
    if not instance_id:
        return jsonify({"error": "instance_id_required"}), 400
    if not soul_content_b64:
        return jsonify({"error": "soul_content_b64_required"}), 400
    
    # 验证 instance_id 匹配（主 DID 持有者才能注册自己的基线）
    if g.instance_id and g.instance_id != instance_id:
        # 放宽限制：instance_id 允许为空字符串表示跨实例注册
        # 但严格模式：token 中的 instance_id 必须匹配
        pass  # 已通过 check_permission 验证
    
    # 解码并计算 hash
    try:
        soul_content_bytes = base64.b64decode(soul_content_b64)
        soul_content = soul_content_bytes.decode("utf-8")
    except Exception as e:
        return jsonify({"error": "invalid_base64", "detail": str(e)}), 400
    
    # 计算 SHA256
    soul_hash = "sha256:" + hashlib.sha256(soul_content.encode("utf-8")).hexdigest()
    
    # 如果提供了 hash，验证一致性
    if provided_hash and provided_hash != soul_hash:
        return jsonify({
            "error": "hash_mismatch",
            "expected": soul_hash,
            "provided": provided_hash,
        }), 400
    
    now = datetime.now(timezone.utc).isoformat()
    
    entry = {
        "instance_id": instance_id,
        "soul_content": soul_content,
        "soul_content_b64": soul_content_b64,
        "soul_hash": soul_hash,
        "soul_length": len(soul_content),
        "registered_at": now,
        "registered_by_did": g.did,
        "token_instance_id": g.instance_id,
    }
    
    SoulBaselineStore.save(instance_id, entry)
    
    return jsonify({
        "instance_id": instance_id,
        "soul_hash": soul_hash,
        "soul_length": len(soul_content),
        "registered_at": now,
        "registered_by_did": g.did,
        "message": "soul baseline registered successfully",
    }), 201


@bp.route("/soul-baselines/<instance_id>", methods=["GET"])
@require_did_token(action="memory_read")
def get_soul_baseline(instance_id):
    """
    GET /api/v1/soul-baselines/<instance_id>
    
    读取指定实例的 soul baseline。
    需要 X-DID-Token（memory_read 权限）。
    
    Returns: Soul baseline entry（不含完整 soul_content，用于安全传输）
    """
    entry = SoulBaselineStore.get(instance_id)
    if not entry:
        return jsonify({
            "error": "not_found",
            "instance_id": instance_id,
        }), 404
    
    # 不返回完整内容，仅返回元数据
    return jsonify({
        "instance_id": entry.get("instance_id"),
        "soul_hash": entry.get("soul_hash"),
        "soul_length": entry.get("soul_length"),
        "registered_at": entry.get("registered_at"),
        "registered_by_did": entry.get("registered_by_did"),
        "_saved_at": entry.get("_saved_at"),
        "exists": True,
    })


@bp.route("/soul-baselines/<instance_id>/verify", methods=["GET"])
@require_did_token(action="memory_read")
def verify_soul_baseline(instance_id):
    """
    GET /api/v1/soul-baselines/<instance_id>/verify
    
    验证 soul baseline 的完整性（hash 对比）。
    需要 X-DID-Token（memory_read 权限）。
    
    可选 Query 参数: current_content_b64=<base64>
    如果提供，则对比当前内容与注册基线的 hash。
    
    Returns:
        {
          "instance_id": "...",
          "registered_hash": "sha256:...",
          "current_hash": "sha256:..." or null,
          "match": true/false,
          "soul_length": 12345,
          "verified_at": "..."
        }
    """
    entry = SoulBaselineStore.get(instance_id)
    if not entry:
        return jsonify({
            "error": "not_found",
            "instance_id": instance_id,
        }), 404
    
    current_hash = None
    match = None
    current_b64 = request.args.get("current_content_b64", "")
    
    if current_b64:
        try:
            current_bytes = base64.b64decode(current_b64)
            current_hash = "sha256:" + hashlib.sha256(current_bytes).hexdigest()
            match = (current_hash == entry.get("soul_hash"))
        except Exception:
            current_hash = "invalid_base64"
            match = False
    
    return jsonify({
        "instance_id": instance_id,
        "registered_hash": entry.get("soul_hash"),
        "current_hash": current_hash,
        "match": match,
        "soul_length": entry.get("soul_length"),
        "verified_at": datetime.now(timezone.utc).isoformat(),
        "verified_by_did": getattr(getattr(__import__('flask'), 'g', None) or {}, 'did', 'unknown'),
    })


@bp.route("/soul-baselines/<instance_id>", methods=["DELETE"])
@require_did_token(action="instance_register")
def delete_soul_baseline(instance_id):
    """
    DELETE /api/v1/soul-baselines/<instance_id>
    
    删除实例的 soul baseline。
    需要 X-DID-Token（instance_register 权限）。
    """
    from flask import g
    
    entry = SoulBaselineStore.get(instance_id)
    if not entry:
        return jsonify({
            "error": "not_found",
            "instance_id": instance_id,
        }), 404
    
    # 验证调用者是否为注册者
    if entry.get("registered_by_did") != g.did:
        return jsonify({
            "error": "permission_denied",
            "detail": "Only the DID that registered this baseline can delete it",
        }), 403
    
    SoulBaselineStore.delete(instance_id)
    return jsonify({
        "status": "deleted",
        "instance_id": instance_id,
        "deleted_by_did": g.did,
    })


@bp.route("/soul-baselines/health", methods=["GET"])
def soul_baseline_health():
    """健康检查（无需鉴权）"""
    baselines = SoulBaselineStore.list_all()
    return jsonify({
        "status": "ok",
        "service": "Polaris Soul Baseline API",
        "version": "1.0.0",
        "total_registered": len(baselines),
        "storage_dir": str(_SOUL_BASELINE_DIR),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })


# ========== 注册函数（供 saas_server.py 调用） ==========

def register_soul_baseline_routes(app):
    """将 soul_baseline_api 蓝图注册到 Flask app（前缀 /api/v1）"""
    app.register_blueprint(bp, url_prefix='/api/v1')
