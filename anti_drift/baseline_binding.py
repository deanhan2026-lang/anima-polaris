"""
Polaris x MeshIdentity 集成模块（真实实现）

核心功能:
1. DID 绑定：Polaris AIInstance ↔ DID subject
2. 实例鉴权：注册/操作时验证 DID 身份
3. 漂移归因：将漂移归因到 DID subject（影响所有相关实例）
4. 批量校准：一次校准，所有该 DID 下的实例同步修正

依赖：mesh_identity_sync 项目（exec 导入，兼容 auth_integration.py 模式）

作者: Nyx | 日期: 2026-07-08
"""

import json
import logging
import sys
from pathlib import Path
from typing import Dict, List, Optional
from datetime import datetime

logger = logging.getLogger(__name__)

# ========== 动态导入（exec 模式，与 auth_integration.py 一致） ==========

def _exec_module(py_path, mod_name, class_name):
    if not py_path.exists():
        raise ImportError(f"模块不存在: {py_path}")
    parent_dir = str(py_path.parent.parent)
    if parent_dir not in sys.path:
        sys.path.insert(0, parent_dir)
    code = open(py_path, encoding="utf-8").read()
    ns = {"__file__": str(py_path), "__name__": mod_name, "__builtins__": __builtins__}
    exec(compile(code, str(py_path), "exec"), ns)
    return ns[class_name]

_WS = Path(__file__).parent.parent.parent
_MESH = _WS / "mesh_identity_sync"

try:
    MultiInstanceDIDManager = _exec_module(
        _MESH / "did" / "multi_instance.py", "multi_instance", "MultiInstanceDIDManager")
except Exception as e:
    logger.error(f"MultiInstanceDIDManager 导入失败: {e}")
    MultiInstanceDIDManager = None

try:
    DIDAuthenticator = _exec_module(
        _MESH / "auth" / "did_auth.py", "did_auth", "DIDAuthenticator")
except Exception as e:
    logger.error(f"DIDAuthenticator 导入失败: {e}")
    DIDAuthenticator = None

try:
    StandardDIDAuth = _exec_module(
        _MESH / "auth" / "standard_did_auth.py", "standard_did_auth", "StandardDIDAuth")
except Exception as e:
    logger.warning(f"StandardDIDAuth 不兼容: {e}")
    StandardDIDAuth = None

try:
    UniversalResolver = _exec_module(
        _MESH / "resolve" / "universal_resolver.py", "universal_resolver", "UniversalResolver")
except Exception as e:
    logger.warning(f"UniversalResolver 不兼容: {e}")
    UniversalResolver = None


# ========== 错误类型 ==========

class DIDBindingError(Exception): pass
class DIDConfigError(Exception): pass


# ========== 常量 ==========

DEFAULT_DID_STORAGE = "Z:/qclaw/did"
DEFAULT_BINDING_STORAGE = "Z:/qclaw/polaris/bindings"
DEFAULT_ATTRIBUTION_STORAGE = "Z:/qclaw/polaris/attributions"
ALLOWED_ACTIONS = {"memory_write", "memory_read", "baseline_admin", "instance_register", "instance_revoke"}

# ========== NAS 离线降级辅助 ==========

def _resolve_path(primary: str, fallback: str) -> Path:
    """尝试主路径(NAS)，不可达则降级到备路径(E盘)"""
    p = Path(primary)
    # 检查 Z:/ 根是否可达
    try:
        if p.drive and p.drive.lower() == 'z:':
            test_root = Path('Z:/')
            if not test_root.exists():
                logger.info(f"NAS(Z:) 不可达，降级到 {fallback}")
                return Path(fallback)
    except Exception:
        logger.info(f"NAS(Z:) 不可达，降级到 {fallback}")
        return Path(fallback)
    p.mkdir(parents=True, exist_ok=True)
    return p


# ========== DIDBindingStore（持久化绑定关系） ==========

class DIDBindingStore:
    """Polaris 实例ID ↔ DID subject 的持久化映射（JSON 旁路存储）"""

    def __init__(self, storage_path: str = DEFAULT_BINDING_STORAGE):
        self.root = _resolve_path(storage_path, "E:/SOFTWARE/qclaw/polaris/bindings")
        self._bindings_file = self.root / "bindings.json"
        self._instance_map_file = self.root / "instance_map.json"
        self._bindings: Dict[str, list] = {}    # did -> [polaris_instance_id, ...]
        self._instance_map: Dict[int, str] = {}  # polaris_instance_id -> did
        self._load()

    def _load(self):
        if self._bindings_file.exists():
            try:
                self._bindings = json.loads(self._bindings_file.read_text(encoding="utf-8"))
            except Exception:
                self._bindings = {}
        if self._instance_map_file.exists():
            try:
                raw = json.loads(self._instance_map_file.read_text(encoding="utf-8"))
                self._instance_map = {int(k): v for k, v in raw.items()}
            except Exception:
                self._instance_map = {}

    def _save(self):
        self._bindings_file.write_text(json.dumps(self._bindings, ensure_ascii=False, indent=2), encoding="utf-8")
        self._instance_map_file.write_text(
            json.dumps({str(k): v for k, v in self._instance_map.items()}, ensure_ascii=False, indent=2),
            encoding="utf-8")

    def bind(self, polaris_instance_id: int, did: str):
        if polaris_instance_id in self._instance_map:
            old_did = self._instance_map[polaris_instance_id]
            if old_did in self._bindings:
                self._bindings[old_did] = [i for i in self._bindings[old_did] if i != polaris_instance_id]
                if not self._bindings[old_did]:
                    del self._bindings[old_did]
        self._bindings.setdefault(did, [])
        if polaris_instance_id not in self._bindings[did]:
            self._bindings[did].append(polaris_instance_id)
        self._instance_map[polaris_instance_id] = did
        self._save()

    def unbind(self, polaris_instance_id: int):
        if polaris_instance_id not in self._instance_map:
            return
        did = self._instance_map.pop(polaris_instance_id)
        if did in self._bindings:
            self._bindings[did] = [i for i in self._bindings[did] if i != polaris_instance_id]
            if not self._bindings[did]:
                del self._bindings[did]
        self._save()

    def get_did(self, polaris_instance_id: int) -> Optional[str]:
        return self._instance_map.get(polaris_instance_id)

    def get_instances(self, did: str) -> List[int]:
        return self._bindings.get(did, [])

    def get_all_bindings(self) -> Dict[str, list]:
        return dict(self._bindings)


# ========== BaselineBindingManager（核心） ==========

class BaselineBindingManager:
    """
    将 Polaris 的人格基线提升到 DID 主体级别。
    连接 Polaris(SQLite) + MeshIdentity(DID) + MemGuard(鉴权) 三个产品。
    """

    def __init__(
        self,
        polaris_base_url: str = "http://127.0.0.1:5052/api/v1",
        polaris_token: str = "",
        did_storage_path: str = DEFAULT_DID_STORAGE,
        binding_storage_path: str = DEFAULT_BINDING_STORAGE,
    ):
        self.polaris_url = polaris_base_url.rstrip("/")
        self.polaris_token = polaris_token
        self.did_storage = _resolve_path(did_storage_path, "E:/SOFTWARE/qclaw/did")
        self.bindings = DIDBindingStore(storage_path=binding_storage_path)
        self._mi_manager = None
        self._did_auth = None
        self._standard_auth = None
        self._resolver = None
        self._inst_cache: Dict[int, dict] = {}

    @property
    def multi_instance(self):
        if self._mi_manager is None:
            if MultiInstanceDIDManager is None:
                raise DIDConfigError("MultiInstanceDIDManager 不可用")
            self._mi_manager = MultiInstanceDIDManager(storage_path=str(self.did_storage))
        return self._mi_manager

    @property
    def did_auth(self):
        if self._did_auth is None:
            if DIDAuthenticator is None:
                raise DIDConfigError("DIDAuthenticator 不可用")
            self._did_auth = DIDAuthenticator(storage_path=str(self.did_storage))
        return self._did_auth

    @property
    def standard_auth(self):
        if self._standard_auth is None and StandardDIDAuth is not None:
            self._standard_auth = StandardDIDAuth(storage_path=str(self.did_storage))
        return self._standard_auth

    @property
    def resolver(self):
        if self._resolver is None and UniversalResolver is not None:
            self._resolver = UniversalResolver()
        return self._resolver

    # ---- Polaris REST API 调用 ----

    def _polaris_request(self, method: str, path: str, body: dict = None) -> dict:
        import urllib.request, urllib.error
        url = f"{self.polaris_url}{path}"
        headers = {"Content-Type": "application/json"}
        if self.polaris_token:
            headers["Authorization"] = f"Bearer {self.polaris_token}"
        data = json.dumps(body).encode("utf-8") if body else None
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body_text = e.read().decode("utf-8", errors="replace")
            raise DIDBindingError(f"Polaris API {e.code}: {body_text}") from e

    def _get_polaris_instance(self, inst_id: int) -> dict:
        if inst_id not in self._inst_cache:
            self._inst_cache[inst_id] = self._polaris_request("GET", f"/instances/{inst_id}")
        return self._inst_cache[inst_id]

    # ---- DID 解析辅助 ----

    def _resolve_did(self, did: str) -> Optional[dict]:
        """尝试解析 DID，失败不阻断"""
        if self.resolver:
            try:
                return self.resolver.resolve(did)
            except Exception as e:
                logger.warning(f"DID 解析失败（继续）: {e}")
        return None

    # ---- 公开 API ----

    def bind_instance_to_did(self, polaris_instance_id: int, did: str, did_token: str = None) -> dict:
        """将 Polaris 实例绑定到 DID subject"""
        if not did.startswith("did:"):
            raise DIDBindingError(f"无效 DID 格式: {did}")

        # 令牌验证
        if did_token and self.standard_auth:
            result = self.standard_auth.verify_response(did_token)
            if not result.get("valid"):
                raise DIDBindingError(f"DID 身份验证失败: {result.get('error', 'unknown')}")

        # DID 解析
        resolved_doc = self._resolve_did(did)
        if resolved_doc:
            logger.info(f"DID {did[:50]}... 解析成功（{len(resolved_doc.get('verificationMethod', []))} 验证方法）")

        # 验证 Polaris 实例存在
        inst = self._get_polaris_instance(polaris_instance_id)

        # 执行绑定
        self.bindings.bind(polaris_instance_id, did)
        logger.info(f"绑定: Polaris 实例 {polaris_instance_id}({inst.get('name')}) -> DID {did[:50]}...")

        return {
            "polaris_instance_id": polaris_instance_id,
            "polaris_instance_name": inst.get("name", ""),
            "did": did,
            "instances_under_did": len(self.bindings.get_instances(did)),
            "bound_at": datetime.now().isoformat(),
        }

    def unbind_instance(self, polaris_instance_id: int) -> dict:
        old_did = self.bindings.get_did(polaris_instance_id)
        if not old_did:
            return {"status": "not_bound", "instance_id": polaris_instance_id}
        self.bindings.unbind(polaris_instance_id)
        return {"status": "unbound", "instance_id": polaris_instance_id, "previous_did": old_did}

    def get_did_status(self, did: str) -> dict:
        instances = self.bindings.get_instances(did)
        details = []
        for inst_id in instances:
            try:
                inst = self._get_polaris_instance(inst_id)
                report = self._polaris_request("GET", f"/instances/{inst_id}/report")
                details.append({
                    "id": inst_id, "name": inst.get("name", ""),
                    "baselines": inst.get("baseline_count", 0),
                    "total_checks": report.get("total_checks", 0),
                    "latest_judgment": report.get("latest", {}).get("judgment", "unknown"),
                    "status": inst.get("status", "unknown"),
                })
            except Exception as e:
                details.append({"id": inst_id, "error": str(e)})
        return {"did": did, "instance_count": len(instances), "instances": details, "queried_at": datetime.now().isoformat()}

    def attribute_drift_to_did(self, polaris_instance_id: int, drift_score: float, dimension_scores: dict, judgment: str) -> dict:
        """将漂移归因到 DID subject"""
        did = self.bindings.get_did(polaris_instance_id)
        if not did:
            return {"status": "not_bound", "message": "该实例未绑定 DID，无法归因"}
        affected = self.bindings.get_instances(did)
        attribution = {
            "drift_instance_id": polaris_instance_id, "primary_did": did,
            "drift_score": drift_score, "dimension_scores": dimension_scores,
            "judgment": judgment, "instances_affected": affected,
            "attributed_at": datetime.now().isoformat(),
        }
        attr_dir = _resolve_path(DEFAULT_ATTRIBUTION_STORAGE, "E:/SOFTWARE/qclaw/polaris/attributions")
        f = attr_dir / f"attr_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}.json"
        f.write_text(json.dumps(attribution, ensure_ascii=False, indent=2), encoding="utf-8")
        logger.info(f"漂移归因 -> DID {did[:50]}... | score={drift_score:.4f} | 影响 {len(affected)} 实例")
        return attribution

    def batch_calibrate_by_did(self, did: str) -> dict:
        polaris_instances = self.bindings.get_instances(did)
        if not polaris_instances:
            return {"status": "no_instances", "did": did}
        results = []
        for inst_id in polaris_instances:
            try:
                inst = self._get_polaris_instance(inst_id)
                try:
                    rx = self._polaris_request("GET", f"/instances/{inst_id}/prescription")
                    has_prescription = True
                except Exception:
                    rx = None
                    has_prescription = False
                results.append({
                    "instance_id": inst_id, "instance_name": inst.get("name", ""),
                    "baseline_count": inst.get("baseline_count", 0), "has_prescription": has_prescription,
                })
            except Exception as e:
                results.append({"instance_id": inst_id, "error": str(e)})

        cal = {"did": did, "total_instances": len(polaris_instances),
               "calibrated": sum(1 for r in results if "error" not in r),
               "instances": results, "calibrated_at": datetime.now().isoformat()}
        cal_dir = _resolve_path("Z:/qclaw/polaris/calibrations", "E:/SOFTWARE/qclaw/polaris/calibrations")
        cal_dir.mkdir(parents=True, exist_ok=True)
        (cal_dir / f"cal_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}.json").write_text(
            json.dumps(cal, ensure_ascii=False, indent=2), encoding="utf-8")
        logger.info(f"DID {did[:50]}... 批量校准: {len(polaris_instances)} 实例")
        return cal

    def get_standalone_report(self, polaris_instance_id: int, include_did: bool = True) -> dict:
        report = {}
        try:
            report["polaris"] = self._polaris_request("GET", f"/instances/{polaris_instance_id}/report")
        except Exception as e:
            report["polaris"] = {"error": str(e)}
        try:
            report["instance"] = self._get_polaris_instance(polaris_instance_id)
        except Exception as e:
            report["instance"] = {"error": str(e)}
        if include_did:
            did = self.bindings.get_did(polaris_instance_id)
            report["did_context"] = {"bound": bool(did)}
            if did:
                report["did_context"]["did"] = did
                report["did_context"]["instances_under_did"] = self.bindings.get_instances(did)
        return report

    def create_did_token(self, primary_did: str, instance_id: str, action: str = "memory_write",
                         expires_in: int = 3600, key_password: str = None) -> str:
        if DIDAuthenticator is None:
            raise DIDConfigError("DIDAuthenticator 不可用")
        auth = DIDAuthenticator(storage_path=str(self.did_storage))
        return auth.create_auth_token(primary_did=primary_did, instance_id=instance_id,
                                      action=action, expires_in=expires_in, password=key_password)


# ========== 工厂函数 ==========

def create_binding_manager(
    polaris_url: str = "http://127.0.0.1:5052/api/v1",
    email: str = "nyx-demo@wlmhan.local",
    password: str = "demo123",
) -> BaselineBindingManager:
    """快速创建管理器（自动登录 Polaris）"""
    import urllib.request
    login_data = json.dumps({"email": email, "password": password}).encode("utf-8")
    req = urllib.request.Request(f"{polaris_url}/auth/login", data=login_data,
                                 headers={"Content-Type": "application/json"})
    token = ""
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            token = json.loads(resp.read().decode("utf-8")).get("access_token", "")
    except Exception as e:
        logger.warning(f"Polaris 登录失败: {e}")
    return BaselineBindingManager(polaris_base_url=polaris_url, polaris_token=token)


# ========== Flask 路由注册 ==========

def register_did_routes(bp):
    """向 Polaris Flask Blueprint 注册 DID 相关路由"""
    from flask import jsonify, request

    def _get_mgr():
        return create_binding_manager()

    @bp.route("/instances/<int:inst_id>/bind-did", methods=["POST"])
    def bind_did(inst_id):
        try:
            mgr = _get_mgr()
            data = request.json or {}
            did = data.get("did", "")
            if not did:
                return jsonify({"error": "did_required"}), 400
            return jsonify(mgr.bind_instance_to_did(inst_id, did)), 200
        except Exception as e:
            return jsonify({"error": str(e)}), 400

    @bp.route("/instances/<int:inst_id>/bind-did", methods=["DELETE"])
    def unbind_did(inst_id):
        try:
            return jsonify(_get_mgr().unbind_instance(inst_id)), 200
        except Exception as e:
            return jsonify({"error": str(e)}), 400

    @bp.route("/instances/<int:inst_id>/did-status")
    def instance_did_status(inst_id):
        try:
            mgr = _get_mgr()
            did = mgr.bindings.get_did(inst_id)
            if not did:
                return jsonify({"bound": False, "instance_id": inst_id})
            return jsonify({
                "bound": True, "instance_id": inst_id, "did": did,
                "instances_under_did": mgr.bindings.get_instances(did)})
        except Exception as e:
            return jsonify({"error": str(e)}), 400

    @bp.route("/did/<path:did>/status")
    def did_status(did):
        try:
            return jsonify(_get_mgr().get_did_status(did))
        except Exception as e:
            return jsonify({"error": str(e)}), 400

    @bp.route("/did/<path:did>/calibrate", methods=["POST"])
    def calibrate_did(did):
        try:
            return jsonify(_get_mgr().batch_calibrate_by_did(did)), 200
        except Exception as e:
            return jsonify({"error": str(e)}), 400

    @bp.route("/bindings")
    def list_bindings():
        try:
            return jsonify(_get_mgr().bindings.get_all_bindings())
        except Exception as e:
            return jsonify({"error": str(e)}), 400

    @bp.route("/instances/<int:inst_id>/did-report")
    def did_report(inst_id):
        try:
            return jsonify(_get_mgr().get_standalone_report(inst_id))
        except Exception as e:
            return jsonify({"error": str(e)}), 400
