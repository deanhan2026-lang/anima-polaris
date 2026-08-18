# -*- coding: utf-8 -*-
"""
anti_drift/billing_api.py
Polaris M3 · 订阅计费 API（P-CODE-003，v0.3）— Blueprint，路由前缀 /api/v1/billing

端点：
- GET    /billing/plans              套餐列表（价格 + 限额，config 权威）
- GET    /billing/subscription       当前订阅
- POST   /billing/subscription       创建订阅（trial 开始）
- POST   /billing/subscription/change   升级 / 降级
- POST   /billing/subscription/cancel   取消订阅
- POST   /billing/usage              记录用量事件
- GET    /billing/usage/summary      周期聚合 + over_limit
- POST   /billing/checkout           创建支付会话（模拟）
- POST   /billing/mock-pay/<id>      模拟支付成功（生成并处理 webhook 事件）
- POST   /billing/webhook            webhook 回调（HMAC 签名验证）
- GET    /billing/invoices           账单（当前周期视图 + 历史支付）
"""
import json

from flask import Blueprint, jsonify, request, g

from . import billing
from .api_v2 import require_auth
from .db import get_db
from .models_saas import BillingCheckout
from common.logger import get_logger

logger = get_logger("anti_drift.billing_api")

bp = Blueprint("billing_api", __name__, url_prefix="/api/v1/billing")


@bp.route("/plans")
@require_auth
def list_plans():
    plans = {}
    for name, cfg in (billing.get_plans() or {}).items():
        plans[name] = {
            "price_monthly": cfg.get("price_monthly", 0),
            "currency": billing.get_currency(),
            "quota": cfg.get("quota", {}) or {},
        }
    return jsonify({"plans": plans})


@bp.route("/subscription")
@require_auth
def get_subscription():
    db = g.db
    sub = billing.get_subscription(db, g.user.id)
    db.commit()  # 惰性过期落库
    return jsonify({"subscription": billing.subscription_dict(sub)})


@bp.route("/subscription", methods=["POST"])
@require_auth
def create_subscription():
    data = request.json or {}
    plan = (data.get("plan") or "free").strip()
    db = g.db
    sub, ok, reason = billing.create_subscription(db, g.user.id, plan)
    if not ok:
        db.rollback()
        return jsonify({"error": reason}), 409
    db.commit()
    return jsonify({"subscription": billing.subscription_dict(sub), "message": reason}), 201


@bp.route("/subscription/change", methods=["POST"])
@require_auth
def change_subscription():
    data = request.json or {}
    new_plan = (data.get("plan") or "").strip()
    if not new_plan:
        return jsonify({"error": "plan_required"}), 400
    db = g.db
    sub = billing.get_subscription(db, g.user.id)
    if sub is None:
        return jsonify({"error": "no_subscription"}), 404
    sub, ok, reason = billing.change_plan(db, sub, new_plan)
    if not ok:
        db.rollback()
        return jsonify({"error": reason}), 409
    db.commit()
    return jsonify({"subscription": billing.subscription_dict(sub)})


@bp.route("/subscription/cancel", methods=["POST"])
@require_auth
def cancel_subscription():
    db = g.db
    sub = billing.get_subscription(db, g.user.id)
    if sub is None:
        return jsonify({"error": "no_subscription"}), 404
    sub, ok, reason = billing.cancel_subscription(db, sub)
    if not ok:
        db.rollback()
        return jsonify({"error": reason}), 409
    db.commit()
    return jsonify({"subscription": billing.subscription_dict(sub), "message": reason})


@bp.route("/usage", methods=["POST"])
@require_auth
def record_usage():
    data = request.json or {}
    event_type = (data.get("event_type") or "").strip()
    quantity = data.get("quantity", 1)
    ai_instance_id = data.get("ai_instance_id")
    db = g.db
    ev, ok, reason = billing.record_usage(db, g.user.id, event_type, quantity, ai_instance_id)
    if not ok:
        db.rollback()
        return jsonify({"error": reason}), 400
    db.commit()
    return jsonify({"id": ev.id, "event_type": ev.event_type, "quantity": ev.quantity}), 201


@bp.route("/usage/summary")
@require_auth
def usage_summary():
    db = g.db
    sub = billing.get_subscription(db, g.user.id)
    plan = sub.plan if sub else "free"
    agg = billing.aggregate_usage(db, g.user.id, plan=plan)
    return jsonify(agg)


@bp.route("/checkout", methods=["POST"])
@require_auth
def checkout():
    data = request.json or {}
    plan = (data.get("plan") or "").strip()
    if not plan:
        return jsonify({"error": "plan_required"}), 400
    db = g.db
    co, ok, reason = billing.create_checkout(db, g.user.id, plan)
    if not ok:
        db.rollback()
        return jsonify({"error": reason}), 400
    db.commit()
    return jsonify({"checkout": billing.checkout_dict(co)}), 201


@bp.route("/mock-pay/<int:checkout_id>", methods=["POST"])
@require_auth
def mock_pay(checkout_id):
    """模拟支付成功：构造 payment_intent.succeeded 事件并走 webhook 处理链路"""
    db = g.db
    co = db.query(BillingCheckout).filter(BillingCheckout.id == checkout_id).first()
    if co is None or co.user_id != g.user.id:
        return jsonify({"error": "checkout_not_found"}), 404
    event = {
        "type": "payment_intent.succeeded",
        "data": {"checkout_id": co.id, "user_id": co.user_id, "plan": co.plan},
    }
    ok, reason, detail = billing.handle_webhook_event(db, event, commit=True)
    if not ok:
        db.rollback()
        return jsonify({"error": reason}), 409
    return jsonify({"ok": True, "detail": detail})


@bp.route("/webhook", methods=["POST"])
def webhook():
    """
    webhook 回调（模拟 Stripe）：
    header: X-Polaris-Signature: t=<ts>,v1=<hmac-sha256>
    body: JSON 事件（原始字节参与签名）
    """
    secret = billing.get_webhook_secret()
    payload = request.get_data()
    sig = request.headers.get("X-Polaris-Signature", "")
    ok, reason = billing.verify_webhook(secret, payload, sig)
    if not ok:
        logger.warning("webhook 签名验证失败: %s", reason)
        return jsonify({"error": f"invalid_signature:{reason}"}), 400
    try:
        event = json.loads(payload.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return jsonify({"error": "invalid_json"}), 400
    db = next(get_db())
    try:
        ok, reason, detail = billing.handle_webhook_event(db, event, commit=True)
        if not ok:
            return jsonify({"error": reason}), 422
        return jsonify({"received": True, "detail": detail})
    finally:
        db.close()


@bp.route("/invoices")
@require_auth
def invoices():
    db = g.db
    sub = billing.get_subscription(db, g.user.id)
    current = billing.build_invoice(db, g.user.id, sub=sub)
    history = billing.list_paid_invoices(db, g.user.id)
    return jsonify({"current": current, "history": history})
