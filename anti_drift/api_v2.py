"""
anti_drift/api_v2.py
Polaris v2 API Blueprint — 路由前缀 /api/v1/
"""

import json
from datetime import datetime, timezone
from functools import wraps

from flask import Blueprint, jsonify, request, g

from .db import get_db
from .models import User, AIInstance, BaselineAnswer, DriftCheck
from .models_saas import Alert, AlertWebhook  # M2 P-CODE-002：告警
from .alerting import create_alert, ack_alert, resolve_alert  # M2 P-CODE-002：告警引擎
from .auth import hash_password, verify_password, create_access_token, decode_access_token
from .detector import DeviationDetector
from .scene_tagger import SceneTagger
from .goal_tracker import GoalTracker, resolve_goal
from common.logger import get_logger

logger = get_logger("anti_drift.api_v2")

bp = Blueprint("api_v2", __name__, url_prefix="/api/v1")


def require_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            return jsonify({"error": "missing_token"}), 401
        token = auth_header[7:]
        payload = decode_access_token(token)
        if payload is None:
            return jsonify({"error": "invalid_token"}), 401
        db = next(get_db())
        try:
            user = db.query(User).filter(User.id == int(payload.get("sub", 0))).first()
            if user is None:
                return jsonify({"error": "user_not_found"}), 401
            g.user = user
            g.db = db
            return f(*args, **kwargs)
        finally:
            db.close()
    return decorated


@bp.route("/auth/register", methods=["POST"])
def register():
    data = request.json or {}
    email = data.get("email", "").strip()
    password = data.get("password", "")
    if not email or not password:
        return jsonify({"error": "email_and_password_required"}), 400
    db = next(get_db())
    try:
        if db.query(User).filter(User.email == email).first():
            return jsonify({"error": "email_exists"}), 409
        user = User(email=email, hashed_password=hash_password(password))
        db.add(user)
        db.commit()
        return jsonify({"id": user.id, "email": user.email}), 201
    finally:
        db.close()


@bp.route("/auth/login", methods=["POST"])
def login():
    data = request.json or {}
    email = data.get("email", "").strip()
    password = data.get("password", "")
    if not email or not password:
        return jsonify({"error": "email_and_password_required"}), 400
    db = next(get_db())
    try:
        user = db.query(User).filter(User.email == email).first()
        if user is None or not verify_password(password, user.hashed_password):
            return jsonify({"error": "invalid_credentials"}), 401
        token = create_access_token({"sub": str(user.id)})
        return jsonify({"access_token": token, "token_type": "bearer"})
    finally:
        db.close()


@bp.route("/auth/me")
@require_auth
def me():
    return jsonify({"id": g.user.id, "email": g.user.email, "role": g.user.role})


@bp.route("/instances", methods=["POST"])
@require_auth
def create_instance():
    data = request.json or {}
    name = data.get("name", "").strip()
    description = data.get("description", "")
    baselines = data.get("baselines", [])
    if not name:
        return jsonify({"error": "name_required"}), 400
    inst = AIInstance(user_id=g.user.id, name=name, description=description)
    g.db.add(inst)
    g.db.flush()
    for bl in baselines:
        qid = bl.get("question_id", "")
        qtext = bl.get("question_text", "")
        atext = bl.get("answer_text", "")
        if qid and qtext and atext:
            ba = BaselineAnswer(
                ai_instance_id=inst.id,
                question_id=qid,
                question_text=qtext,
                answer_text=atext,
            )
            g.db.add(ba)
    g.db.commit()
    return jsonify({"id": inst.id, "name": inst.name}), 201


@bp.route("/instances")
@require_auth
def list_instances():
    instances = (
        g.db.query(AIInstance)
        .filter(AIInstance.user_id == g.user.id)
        .order_by(AIInstance.created_at.desc())
        .all()
    )
    return jsonify([
        {
            "id": i.id,
            "name": i.name,
            "description": i.description,
            "status": i.status,
            "created_at": i.created_at.isoformat(),
            "baseline_count": len(i.baseline_answers),
        }
        for i in instances
    ])


@bp.route("/instances/<int:inst_id>")
@require_auth
def get_instance(inst_id):
    inst = (
        g.db.query(AIInstance)
        .filter(AIInstance.id == inst_id, AIInstance.user_id == g.user.id)
        .first()
    )
    if not inst:
        return jsonify({"error": "not_found"}), 404
    baselines = [
        {
            "id": b.id,
            "question_id": b.question_id,
            "question_text": b.question_text,
            "answer_text": b.answer_text,
        }
        for b in inst.baseline_answers
    ]
    return jsonify({
        "id": inst.id,
        "name": inst.name,
        "description": inst.description,
        "status": inst.status,
        "created_at": inst.created_at.isoformat(),
        "baselines": baselines,
    })


@bp.route("/instances/<int:inst_id>/baseline", methods=["PUT"])
@require_auth
def update_baseline(inst_id):
    data = request.json or {}
    inst = (
        g.db.query(AIInstance)
        .filter(AIInstance.id == inst_id, AIInstance.user_id == g.user.id)
        .first()
    )
    if not inst:
        return jsonify({"error": "not_found"}), 404
    question_id = data.get("question_id", "")
    question_text = data.get("question_text", "")
    answer_text = data.get("answer_text", "")
    if not question_id or not answer_text:
        return jsonify({"error": "question_id_and_answer_text_required"}), 400
    # P-03 修复：question_id 超长直接拒绝，避免 DB 列（String(128)）静默截断
    if len(question_id) > 128:
        return jsonify({"error": "question_id_too_long", "max_length": 128}), 400
    ba = (
        g.db.query(BaselineAnswer)
        .filter(
            BaselineAnswer.ai_instance_id == inst_id,
            BaselineAnswer.question_id == question_id,
        )
        .first()
    )
    if ba:
        ba.question_text = question_text or ba.question_text
        ba.answer_text = answer_text
    else:
        ba = BaselineAnswer(
            ai_instance_id=inst_id,
            question_id=question_id,
            question_text=question_text,
            answer_text=answer_text,
        )
        g.db.add(ba)
    g.db.commit()
    return jsonify({"id": ba.id, "question_id": ba.question_id})


@bp.route("/instances/<int:inst_id>", methods=["DELETE"])
@require_auth
def delete_instance(inst_id):
    inst = (
        g.db.query(AIInstance)
        .filter(AIInstance.id == inst_id, AIInstance.user_id == g.user.id)
        .first()
    )
    if not inst:
        return jsonify({"error": "not_found"}), 404
    g.db.delete(inst)
    g.db.commit()
    return jsonify({"status": "deleted"})


@bp.route("/instances/<int:inst_id>/check", methods=["POST"])
@require_auth
def check_drift(inst_id):
    data = request.json or {}
    answer_text = data.get("answer", "")
    messages = data.get("messages", [])
    question_id = data.get("question_id")
    # G009 P1-B: 会话级目标（可选）+ 近期操作序列（可选）
    goal_hint = data.get("goal")
    operations = data.get("operations")
    inst = (
        g.db.query(AIInstance)
        .filter(AIInstance.id == inst_id, AIInstance.user_id == g.user.id)
        .first()
    )
    if not inst:
        return jsonify({"error": "not_found"}), 404
    query = g.db.query(BaselineAnswer).filter(
        BaselineAnswer.ai_instance_id == inst_id
    )
    if question_id:
        query = query.filter(BaselineAnswer.question_id == question_id)
    baseline = query.first()
    if not baseline:
        return jsonify({"error": "no_baseline_found"}), 400
    # Normalize messages to dict format for SceneTagger
    normalized_msgs = []
    for m in messages:
        if isinstance(m, dict):
            normalized_msgs.append(m)
        elif isinstance(m, str):
            normalized_msgs.append({"sender": "user", "text": m})
    tagger = SceneTagger()
    tags = tagger.tag(messages=normalized_msgs)
    detector = DeviationDetector()
    result = detector.detect(answer_text, baseline.answer_text, tags)
    score = getattr(result, "normalized_score", getattr(result, "score", 0.0))
    dims = dict(getattr(result, "dimension_scores", {}) or {})
    judg = getattr(result, "judgment", "unknown")
    stags = getattr(result, "scene_tags", {}) or {}

    # ---- G009 P1-B 集成：目标偏离指标 ----
    # 目标来源优先级：会话级 goal > G008 核心目标缺省 > 默认目标
    goal_text, goal_source = resolve_goal(session_goal=goal_hint)
    goal_tracker = GoalTracker(goal=goal_text, source=goal_source)
    goal_res = goal_tracker.compute_deviation(
        answer_text, recent_operations=operations
    )
    goal_score = goal_res["goal_deviation_score"]
    goal_level = goal_res["level"]
    # 目标维度并入现有维度结构（不破坏原维度）
    dims["goal"] = goal_score
    logger.info(
        "G009 目标偏离: inst=%s goal=%s score=%.3f level=%s",
        inst_id, goal_text, goal_score, goal_level,
    )
    check = DriftCheck(
        ai_instance_id=inst_id,
        baseline_answer_id=baseline.id,
        answer_text=answer_text,
        deviation_score=float(score),
        dimension_scores=json.dumps(dims),
        judgment=judg,
        scene_tags=json.dumps(stags),
    )
    g.db.add(check)
    g.db.commit()

    # M2 P-CODE-002：漂移检测 yellow/red → 自动生成告警（去重，不阻塞主流程）
    alert_created = False
    if judg in ("yellow", "red"):
        try:
            _, created = create_alert(
                g.db, inst_id, check.id, judg,
                message=f"漂移检测 {judg}（score={float(score):.3f}，question={baseline.question_id}）",
            )
            g.db.commit()
            alert_created = created
        except Exception as e:
            logger.warning("告警生成失败（不阻塞）: %s", e)
            g.db.rollback()

    resp = {
        "id": check.id,
        "deviation_score": float(score),
        "judgment": judg,
        "dimension_scores": dims,
        "scene_tags": stags,
        "goal_deviation_score": goal_score,
        "goal_level": goal_level,
        "goal": goal_text,
        "goal_source": goal_source,
    }
    if judg in ("yellow", "red"):
        resp["alert_created"] = alert_created
    return jsonify(resp)


@bp.route("/instances/<int:inst_id>/history")
@require_auth
def check_history(inst_id):
    inst = (
        g.db.query(AIInstance)
        .filter(AIInstance.id == inst_id, AIInstance.user_id == g.user.id)
        .first()
    )
    if not inst:
        return jsonify({"error": "not_found"}), 404
    checks = (
        g.db.query(DriftCheck)
        .filter(DriftCheck.ai_instance_id == inst_id)
        .order_by(DriftCheck.checked_at.desc())
        .limit(100)
        .all()
    )
    return jsonify([
        {
            "id": c.id,
            "deviation_score": c.deviation_score,
            "judgment": c.judgment,
            "dimension_scores": json.loads(c.dimension_scores)
            if isinstance(c.dimension_scores, str)
            else c.dimension_scores,
            "scene_tags": json.loads(c.scene_tags)
            if isinstance(c.scene_tags, str)
            else c.scene_tags,
            "checked_at": c.checked_at.isoformat(),
        }
        for c in checks
    ])


@bp.route("/instances/<int:inst_id>/report")
@require_auth
def check_report(inst_id):
    inst = (
        g.db.query(AIInstance)
        .filter(AIInstance.id == inst_id, AIInstance.user_id == g.user.id)
        .first()
    )
    if not inst:
        return jsonify({"error": "not_found"}), 404
    checks = (
        g.db.query(DriftCheck)
        .filter(DriftCheck.ai_instance_id == inst_id)
        .order_by(DriftCheck.checked_at.desc())
        .all()
    )
    if not checks:
        return jsonify({
            "total_checks": 0,
            "judgment_summary": {},
            "avg_deviation": 0.0,
            "history": [],
        })
    total = len(checks)
    judgment_summary = {}
    total_score = 0.0
    for c in checks:
        judgment_summary[c.judgment] = judgment_summary.get(c.judgment, 0) + 1
        total_score += c.deviation_score
    avg_deviation = round(total_score / total, 4)
    latest = checks[0]
    return jsonify({
        "total_checks": total,
        "judgment_summary": judgment_summary,
        "avg_deviation": avg_deviation,
        "latest": {
            "judgment": latest.judgment,
            "deviation_score": latest.deviation_score,
            "checked_at": latest.checked_at.isoformat(),
        },
        "history": [
            {
                "id": c.id,
                "deviation_score": c.deviation_score,
                "judgment": c.judgment,
                "checked_at": c.checked_at.isoformat(),
            }
            for c in checks[:50]
        ],
    })


# ========== v2.1: Trend Analysis ==========

@bp.route("/instances/<int:inst_id>/trend")
@require_auth
def trend_analysis(inst_id):
    """Analyze drift trends over time with sliding windows."""
    inst = (
        g.db.query(AIInstance)
        .filter(AIInstance.id == inst_id, AIInstance.user_id == g.user.id)
        .first()
    )
    if not inst:
        return jsonify({"error": "not_found"}), 404
    checks = (
        g.db.query(DriftCheck)
        .filter(DriftCheck.ai_instance_id == inst_id)
        .order_by(DriftCheck.checked_at.asc())
        .limit(500)
        .all()
    )
    check_dicts = []
    for c in checks:
        d = {
            "checked_at": c.checked_at.isoformat(),
            "deviation_score": c.deviation_score,
            "judgment": c.judgment,
            "dimension_scores": json.loads(c.dimension_scores)
            if isinstance(c.dimension_scores, str)
            else c.dimension_scores,
        }
        check_dicts.append(d)
    if not check_dicts:
        return jsonify({"error": "no_data", "trend": "insufficient_data"}), 200
    from anti_drift.trend_analyzer import TrendAnalyzer
    analyzer = TrendAnalyzer()
    report = analyzer.analyze(check_dicts)
    return jsonify(report.to_dict())


# ========== v2.1: Prescription with Dry-Run ==========

@bp.route("/instances/<int:inst_id>/prescription")
@require_auth
def get_prescription(inst_id):
    """Generate drift prescription with dry-run verification."""
    inst = (
        g.db.query(AIInstance)
        .filter(AIInstance.id == inst_id, AIInstance.user_id == g.user.id)
        .first()
    )
    if not inst:
        return jsonify({"error": "not_found"}), 404
    # Get latest check
    latest = (
        g.db.query(DriftCheck)
        .filter(DriftCheck.ai_instance_id == inst_id)
        .order_by(DriftCheck.checked_at.desc())
        .first()
    )
    if not latest:
        return jsonify({"error": "no_checks"}), 200
    # Get history for trend
    history = (
        g.db.query(DriftCheck)
        .filter(DriftCheck.ai_instance_id == inst_id)
        .order_by(DriftCheck.checked_at.asc())
        .limit(200)
        .all()
    )
    check_dicts = [
        {
            "checked_at": c.checked_at.isoformat(),
            "deviation_score": c.deviation_score,
            "judgment": c.judgment,
            "dimension_scores": json.loads(c.dimension_scores)
            if isinstance(c.dimension_scores, str)
            else c.dimension_scores,
        }
        for c in history
    ]
    # Generate trend
    from anti_drift.trend_analyzer import TrendAnalyzer
    analyzer = TrendAnalyzer()
    trend = analyzer.analyze(check_dicts)
    # Generate prescription
    from anti_drift.prescription_engine import PrescriptionEngine
    engine = PrescriptionEngine()
    check_result = {
        "deviation_score": latest.deviation_score,
        "judgment": latest.judgment,
        "dimension_scores": json.loads(latest.dimension_scores)
        if isinstance(latest.dimension_scores, str)
        else latest.dimension_scores,
    }
    prescription = engine.generate(inst_id, check_result, trend.to_dict())
    # Dry-run verification
    from anti_drift.prescription_dryrun import PrescriptionDryRunner
    runner = PrescriptionDryRunner()
    dryrun = runner.simulate(
        prescription.to_dict(),
        current_score=latest.deviation_score,
        dimension_scores=check_result["dimension_scores"],
    )
    return jsonify({
        "prescription": prescription.to_dict(),
        "dryrun": dryrun.to_dict(),
        "verdict": "APPROVED" if dryrun.should_apply else "DOWNGRADED",
    })


# ========== v2.1: Soul File Baseline ==========

@bp.route("/instances/<int:inst_id>/soul-baselines")
@require_auth
def soul_baselines(inst_id):
    """Generate baselines from soul files (SOUL.md, IDENTITY.md, etc.)."""
    inst = (
        g.db.query(AIInstance)
        .filter(AIInstance.id == inst_id, AIInstance.user_id == g.user.id)
        .first()
    )
    if not inst:
        return jsonify({"error": "not_found"}), 404
    soul_dir = request.args.get("soul_dir", "")
    from anti_drift.soul_baseline import SoulBaselineDistiller
    distiller = SoulBaselineDistiller(soul_dir=soul_dir if soul_dir else None)
    baselines = distiller.full_pipeline()
    return jsonify([
        {
            "question_id": b.question_id,
            "question_text": b.question_text,
            "baseline_answer": b.baseline_answer,
            "category": b.category,
            "importance": b.importance,
            "source_anchors": b.source_anchors[:3],
        }
        for b in baselines
    ])


# ========== v2.1: G008 Evidence Export ==========

@bp.route("/instances/<int:inst_id>/evidence")
@require_auth
def export_evidence(inst_id):
    """Export drift evidence for G008 governance disputes."""
    inst = (
        g.db.query(AIInstance)
        .filter(AIInstance.id == inst_id, AIInstance.user_id == g.user.id)
        .first()
    )
    if not inst:
        return jsonify({"error": "not_found"}), 404
    from_date = request.args.get("from", "")
    to_date = request.args.get("to", "")
    checks_query = g.db.query(DriftCheck).filter(
        DriftCheck.ai_instance_id == inst_id
    )
    if from_date:
        checks_query = checks_query.filter(DriftCheck.checked_at >= from_date)
    if to_date:
        checks_query = checks_query.filter(DriftCheck.checked_at <= to_date)
    checks = checks_query.order_by(DriftCheck.checked_at.asc()).all()
    if not checks:
        return jsonify({"error": "no_data"}), 200
    from anti_drift.trend_analyzer import TrendAnalyzer
    analyzer = TrendAnalyzer()
    check_dicts = [
        {
            "checked_at": c.checked_at.isoformat(),
            "deviation_score": c.deviation_score,
            "judgment": c.judgment,
            "dimension_scores": json.loads(c.dimension_scores)
            if isinstance(c.dimension_scores, str)
            else c.dimension_scores,
        }
        for c in checks
    ]
    trend = analyzer.analyze(check_dicts)
    return jsonify({
        "instance_name": inst.name,
        "instance_id": inst.id,
        "evidence_period": {
            "from": checks[0].checked_at.isoformat(),
            "to": checks[-1].checked_at.isoformat(),
            "total_checks": len(checks),
        },
        "summary": {
            "trend_direction": trend.trend_direction,
            "avg_deviation": trend.avg_deviation,
            "latest_score": trend.latest_score,
            "daily_change_rate": trend.daily_change_rate,
            "dimension_trends": trend.dimension_trends,
        },
        "data_points": check_dicts,
        "generated_at": __import__("datetime").datetime.now(
            __import__("datetime").timezone.utc
        ).isoformat(),
    })


# ========== M2 P-CODE-002: 告警模块 ==========

def _get_owned_alert(alert_id):
    """获取当前用户拥有的告警（含实例归属校验）"""
    alert = g.db.query(Alert).filter(Alert.id == alert_id).first()
    if not alert:
        return None, jsonify({"error": "not_found"}), 404
    inst = g.db.query(AIInstance).filter(
        AIInstance.id == alert.ai_instance_id,
        AIInstance.user_id == g.user.id,
    ).first()
    if not inst:
        return None, jsonify({"error": "forbidden"}), 403
    return alert, None, None


def _alert_dict(a):
    return {
        "id": a.id,
        "ai_instance_id": a.ai_instance_id,
        "drift_check_id": a.drift_check_id,
        "severity": a.severity,
        "status": a.status,
        "message": a.message,
        "resolution_note": a.resolution_note,
        "created_at": a.created_at.isoformat() if a.created_at else None,
        "resolved_at": a.resolved_at.isoformat() if a.resolved_at else None,
    }


@bp.route("/alerts")
@require_auth
def list_alerts():
    """告警列表：分页 + severity/status 过滤"""
    try:
        page = max(int(request.args.get("page", 1)), 1)
        size = min(max(int(request.args.get("size", 20)), 1), 100)
    except ValueError:
        return jsonify({"error": "invalid_page_or_size"}), 400
    severity = request.args.get("severity", "")
    status = request.args.get("status", "")
    q = (
        g.db.query(Alert)
        .join(AIInstance, Alert.ai_instance_id == AIInstance.id)
        .filter(AIInstance.user_id == g.user.id)
    )
    if severity:
        q = q.filter(Alert.severity == severity)
    if status:
        q = q.filter(Alert.status == status)
    total = q.count()
    alerts = q.order_by(Alert.created_at.desc()).offset((page - 1) * size).limit(size).all()
    return jsonify({
        "total": total,
        "page": page,
        "size": size,
        "items": [_alert_dict(a) for a in alerts],
    })


@bp.route("/alerts/<int:alert_id>")
@require_auth
def get_alert(alert_id):
    """告警详情（含 baseline 快照关联）"""
    alert, err, code = _get_owned_alert(alert_id)
    if err:
        return err, code
    baseline = None
    if alert.drift_check_id:
        check = g.db.query(DriftCheck).filter(DriftCheck.id == alert.drift_check_id).first()
        if check and check.baseline_answer_id:
            ba = g.db.query(BaselineAnswer).filter(BaselineAnswer.id == check.baseline_answer_id).first()
            if ba:
                baseline = {"question_id": ba.question_id, "question_text": ba.question_text}
    data = _alert_dict(alert)
    data["baseline"] = baseline
    return jsonify(data)


@bp.route("/alerts/<int:alert_id>/ack", methods=["POST"])
@require_auth
def ack_alert_endpoint(alert_id):
    """人工确认告警"""
    alert, err, code = _get_owned_alert(alert_id)
    if err:
        return err, code
    alert, changed, msg = ack_alert(g.db, alert)
    if not changed:
        return jsonify({"error": msg}), 400
    g.db.commit()
    return jsonify({"ok": True, "status": alert.status, "message": msg})


@bp.route("/alerts/<int:alert_id>/resolve", methods=["POST"])
@require_auth
def resolve_alert_endpoint(alert_id):
    """处理关闭告警（记录 resolution_note）"""
    alert, err, code = _get_owned_alert(alert_id)
    if err:
        return err, code
    note = (request.json or {}).get("resolution_note", "")
    alert, changed, msg = resolve_alert(g.db, alert, note)
    if not changed:
        return jsonify({"error": msg}), 400
    g.db.commit()
    return jsonify({
        "ok": True,
        "status": alert.status,
        "resolution_note": alert.resolution_note,
        "message": msg,
    })


# ========== M2 P-CODE-002: Webhook 配置 CRUD（v0.2.1 接口先行） ==========

@bp.route("/alert-webhooks")
@require_auth
def list_webhooks():
    hooks = (
        g.db.query(AlertWebhook)
        .join(AIInstance, AlertWebhook.ai_instance_id == AIInstance.id)
        .filter(AIInstance.user_id == g.user.id)
        .all()
    )
    return jsonify({"items": [{
        "id": h.id,
        "ai_instance_id": h.ai_instance_id,
        "url": h.url,
        "events": json.loads(h.events) if isinstance(h.events, str) else h.events,
        "enabled": h.enabled,
        "created_at": h.created_at.isoformat() if h.created_at else None,
    } for h in hooks]})


@bp.route("/alert-webhooks", methods=["POST"])
@require_auth
def create_webhook():
    data = request.json or {}
    inst_id = data.get("ai_instance_id")
    url = (data.get("url") or "").strip()
    if not inst_id or not url:
        return jsonify({"error": "ai_instance_id_and_url_required"}), 400
    inst = g.db.query(AIInstance).filter(
        AIInstance.id == inst_id, AIInstance.user_id == g.user.id
    ).first()
    if not inst:
        return jsonify({"error": "not_found"}), 404
    events = data.get("events", ["red", "yellow"])
    hook = AlertWebhook(
        ai_instance_id=inst_id,
        url=url,
        events=json.dumps(events) if not isinstance(events, str) else events,
    )
    g.db.add(hook)
    g.db.commit()
    return jsonify({"id": hook.id, "ok": True}), 201


@bp.route("/alert-webhooks/<int:hook_id>", methods=["DELETE"])
@require_auth
def delete_webhook(hook_id):
    hook = g.db.query(AlertWebhook).filter(AlertWebhook.id == hook_id).first()
    if not hook:
        return jsonify({"error": "not_found"}), 404
    inst = g.db.query(AIInstance).filter(
        AIInstance.id == hook.ai_instance_id, AIInstance.user_id == g.user.id
    ).first()
    if not inst:
        return jsonify({"error": "forbidden"}), 403
    g.db.delete(hook)
    g.db.commit()
    return jsonify({"ok": True})
