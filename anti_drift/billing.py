# -*- coding: utf-8 -*-
"""
anti_drift/billing.py
Polaris M3 · 订阅计费引擎（P-CODE-003，v0.3）

职责：
- 订阅状态机：trial / active / canceled / expired + 非法迁移防护
- 用量事件记录 + 月周期聚合（月对齐 + over_limit flag）
- 模拟支付：checkout 会话 + HMAC-SHA256 webhook 签名验证（接口契约先行，无需真实 Stripe）
- 账单视图：当前周期用量 × 套餐价格 + 超额计费（overage）

约束：
- 不破坏存量 4 表 + M1/M2 新表；新增 billing_checkouts（v0.3）
- config.yaml 为套餐价格 / 用量限额 / trial 天数 / webhook secret 的权威来源（config.get 点号路径）
"""
import hashlib
import hmac
import logging
import time
from datetime import datetime, timedelta, timezone

from .config import get as cfg_get
from .models_saas import Subscription, UsageEvent, BillingCheckout

logger = logging.getLogger("anti_drift.billing")

# 订阅状态机（合法迁移表；非法迁移一律拒绝）
SUBSCRIPTION_STATUSES = ("trial", "active", "canceled", "expired")
VALID_TRANSITIONS = {
    "trial": {"active", "canceled", "expired"},
    "active": {"canceled", "expired"},
    "canceled": {"active", "expired"},   # 周期结束 → expired；续费/重购 → active
    "expired": {"active"},               # 过期后重新订阅 → active（走新 checkout）
}

# 用量事件类型（与 config.yaml billing.plans.*.quota 键对齐）
USAGE_EVENT_TYPES = ("detection", "snapshot", "alert_push")


def now_utc():
    return datetime.now(timezone.utc)


def _month_bounds(ref=None):
    """月周期对齐：返回 (period_start, period_end)，取 ref 所在自然月（UTC）"""
    ref = ref or now_utc()
    if ref.tzinfo is None:
        ref = ref.replace(tzinfo=timezone.utc)
    start = ref.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if start.month == 12:
        end = start.replace(year=start.year + 1, month=1)
    else:
        end = start.replace(month=start.month + 1)
    return start, end


def _dt(v):
    if v is None:
        return None
    if v.tzinfo is None:
        return v.replace(tzinfo=timezone.utc)
    return v


# ─────────────────────────── 套餐与限额（config 权威） ───────────────────────────

def get_plans():
    """从 config.yaml 读取套餐定义（价格 + 用量限额），环境变量可覆盖"""
    plans = cfg_get("billing.plans", {}) or {}
    return plans


def get_plan(plan):
    return (get_plans() or {}).get(plan, {})


def get_quota(plan, event_type):
    """套餐限额；按完整点路径读取（支持 POLARIS_BILLING_PLANS_*_QUOTA_* 环境变量逐项覆盖）
    未配置的事件类型视为不限额（None）"""
    return cfg_get(f"billing.plans.{plan}.quota.{event_type}")


def get_price(plan):
    return float(get_plan(plan).get("price_monthly", 0.0))


def get_trial_days():
    return int(cfg_get("billing.trial_days", 14) or 14)


def get_currency():
    return str(cfg_get("billing.currency", "CNY") or "CNY")


def get_webhook_secret():
    return str(cfg_get("billing.webhook_secret", "polaris_dev_whsec") or "polaris_dev_whsec")


def get_overage_rate(event_type):
    rates = cfg_get("billing.overage_rate", {}) or {}
    return float(rates.get(event_type, 0.01))


# ─────────────────────────── 订阅状态机 ───────────────────────────

def transition(db, sub, new_status):
    """
    状态迁移（含非法迁移防护）。

    Returns:
        (sub, ok: bool, reason: str)
        - ok=True 迁移成功（调用方负责 commit）
        - ok=False reason 为失败原因（already_xxx / illegal_transition:old->new）
    """
    if sub.status not in SUBSCRIPTION_STATUSES:
        return sub, False, f"unknown_status:{sub.status}"
    if sub.status == new_status:
        return sub, False, f"already_{new_status}"
    if new_status not in VALID_TRANSITIONS.get(sub.status, set()):
        return sub, False, f"illegal_transition:{sub.status}->{new_status}"
    old = sub.status
    sub.status = new_status
    sub.updated_at = now_utc()
    if new_status == "expired":
        sub.current_period_end = now_utc()
    logger.info("subscription %s: %s -> %s", sub.id, old, new_status)
    return sub, True, "ok"


def create_subscription(db, user_id, plan):
    """
    创建订阅（新订阅从 trial 开始，trial_days 来自 config）。
    若已有 canceled / expired 订阅则重新激活（换新套餐 + 新周期）。

    Returns:
        (sub, ok, reason)；ok=False 时 reason 为错误原因（已有有效订阅等）
    """
    if plan not in get_plans():
        return None, False, f"unknown_plan:{plan}"
    existing = (
        db.query(Subscription)
        .filter(Subscription.user_id == user_id)
        .order_by(Subscription.id.desc())
        .first()
    )
    if existing and existing.status in ("trial", "active"):
        return existing, False, f"subscription_already_{existing.status}"
    if existing and existing.status == "canceled":
        # 取消后重购：直接从 canceled -> active（新周期），等价重新订阅
        sub, ok, reason = transition(db, existing, "active")
        if not ok:
            return existing, False, reason
        existing.plan = plan
        _renew_period(existing)
        db.flush()
        return existing, True, "reactivated"
    if existing and existing.status == "expired":
        sub, ok, reason = transition(db, existing, "active")
        if not ok:
            return existing, False, reason
        existing.plan = plan
        _renew_period(existing)
        db.flush()
        return existing, True, "resubscribed"

    now = now_utc()
    sub = Subscription(
        user_id=user_id,
        plan=plan,
        status="trial",
        current_period_start=now,
        current_period_end=now + timedelta(days=get_trial_days()),
    )
    db.add(sub)
    db.flush()
    return sub, True, "created"


def _renew_period(sub, months=1):
    """续期：从当前周期结束点顺延 months 个月（无则从现在起）"""
    base = _dt(sub.current_period_end) or now_utc()
    new_end = base + timedelta(days=30 * months)
    sub.current_period_start = _dt(sub.current_period_end) or now_utc()
    sub.current_period_end = new_end


def cancel_subscription(db, sub):
    """取消订阅：trial / active -> canceled（周期结束时自动过期）"""
    return transition(db, sub, "canceled")


def expire_if_due(db, sub):
    """
    惰性过期：周期已结束的 trial / active / canceled 自动转 expired。
    返回 (sub, changed: bool)
    """
    if sub.status not in ("trial", "active", "canceled"):
        return sub, False
    end = _dt(sub.current_period_end)
    if end is not None and end <= now_utc():
        sub, ok, _ = transition(db, sub, "expired")
        return sub, ok
    return sub, False


def activate_subscription(db, sub):
    """支付成功后激活：trial -> active（续费时顺延周期）"""
    if sub.status == "active":
        # 续费：周期顺延
        _renew_period(sub)
        sub.updated_at = now_utc()
        db.flush()
        return sub, True, "renewed"
    if sub.status in ("canceled", "expired"):
        sub, ok, reason = transition(db, sub, "active")
        if not ok:
            return sub, False, reason
        _renew_period(sub)
        db.flush()
        return sub, True, "reactivated"
    sub, ok, reason = transition(db, sub, "active")
    if not ok:
        return sub, False, reason
    _renew_period(sub)
    db.flush()
    return sub, True, "activated"


def change_plan(db, sub, new_plan):
    """升级 / 降级：active / trial 状态允许换套餐（立即生效，下一周期按新价计费）"""
    if new_plan not in get_plans():
        return sub, False, f"unknown_plan:{new_plan}"
    if sub.plan == new_plan:
        return sub, False, "plan_unchanged"
    if sub.status not in ("trial", "active"):
        return sub, False, f"cannot_change_plan_in_{sub.status}"
    sub.plan = new_plan
    sub.updated_at = now_utc()
    db.flush()
    return sub, True, "ok"


def get_subscription(db, user_id, lazy_expire=True):
    """用户当前订阅（惰性过期后返回；无订阅返回 None）"""
    sub = (
        db.query(Subscription)
        .filter(Subscription.user_id == user_id)
        .order_by(Subscription.id.desc())
        .first()
    )
    if sub is None:
        return None
    if lazy_expire:
        expire_if_due(db, sub)
    return sub


def subscription_dict(sub):
    if sub is None:
        return None
    return {
        "id": sub.id,
        "plan": sub.plan,
        "status": sub.status,
        "current_period_start": _dt(sub.current_period_start).isoformat() if sub.current_period_start else None,
        "current_period_end": _dt(sub.current_period_end).isoformat() if sub.current_period_end else None,
        "created_at": _dt(sub.created_at).isoformat() if sub.created_at else None,
        "updated_at": _dt(sub.updated_at).isoformat() if sub.updated_at else None,
    }


# ─────────────────────────── 用量记录与聚合 ───────────────────────────

def record_usage(db, user_id, event_type, quantity=1, ai_instance_id=None):
    """
    记录用量事件。

    Returns:
        (event, ok, reason)；event_type 非法或 quantity<1 时 ok=False
    """
    if event_type not in USAGE_EVENT_TYPES:
        return None, False, f"unknown_event_type:{event_type}"
    try:
        quantity = int(quantity)
    except (TypeError, ValueError):
        return None, False, "invalid_quantity"
    if quantity < 1:
        return None, False, "invalid_quantity"
    ev = UsageEvent(
        user_id=user_id,
        ai_instance_id=ai_instance_id,
        event_type=event_type,
        quantity=quantity,
    )
    db.add(ev)
    db.flush()
    return ev, True, "ok"


def aggregate_usage(db, user_id, plan=None, period_start=None, period_end=None):
    """
    周期用量聚合：按 event_type 求和，对照套餐限额给出 over_limit。

    周期缺省时按自然月对齐（UTC 当月 1 号 ~ 下月 1 号）；若传入订阅则优先用订阅周期。
    若 plan 为 None，读取用户当前订阅套餐（无订阅按 free）。

    Returns:
        {
          "period": {"start": iso, "end": iso},
          "plan": "pro",
          "items": [{"event_type": ..., "used": n, "quota": q, "over": n-q, "over_limit": bool}],
          "overall_over_limit": bool,
        }
    """
    if plan is None:
        plan = "free"
    if period_start is None or period_end is None:
        period_start, period_end = _month_bounds()

    rows = (
        db.query(UsageEvent)
        .filter(
            UsageEvent.user_id == user_id,
            UsageEvent.created_at >= period_start,
            UsageEvent.created_at < period_end,
        )
        .all()
    )
    used_by_type = {}
    for ev in rows:
        used_by_type[ev.event_type] = used_by_type.get(ev.event_type, 0) + (ev.quantity or 0)

    items = []
    overall = False
    for et in USAGE_EVENT_TYPES:
        used = used_by_type.get(et, 0)
        quota = get_quota(plan, et)
        if quota is None:
            over, over_limit = 0, False
        else:
            over = max(0, used - quota)
            over_limit = used > quota
        items.append({
            "event_type": et,
            "used": used,
            "quota": quota,
            "over": over,
            "over_limit": over_limit,
        })
        if over_limit:
            overall = True

    return {
        "period": {
            "start": period_start.isoformat(),
            "end": period_end.isoformat(),
        },
        "plan": plan,
        "items": items,
        "overall_over_limit": overall,
    }


# ─────────────────────────── 模拟支付（checkout + webhook） ───────────────────────────

def sign_webhook(secret, payload, timestamp=None):
    """
    webhook 签名：HMAC-SHA256(secret, f"{ts}." + payload) → "t=<ts>,v1=<hex>"
    payload 为 bytes。与 Stripe 的 X-Stripe-Signature 格式对齐（模拟契约）。
    """
    ts = str(int(timestamp if timestamp is not None else time.time()))
    msg = ts.encode("utf-8") + b"." + payload
    digest = hmac.new(secret.encode("utf-8"), msg, hashlib.sha256).hexdigest()
    return f"t={ts},v1={digest}"


def verify_webhook(secret, payload, signature_header, max_age=300):
    """
    校验 webhook 签名（模拟签名可验证）：
    - header 必须为 "t=<ts>,v1=<hex>" 格式
    - 时间戳新鲜度（|now - ts| <= max_age）
    - HMAC 恒定时间比较

    Returns: (ok: bool, reason: str)
    """
    if not signature_header:
        return False, "missing_signature"
    parts = {}
    for kv in signature_header.split(","):
        if "=" in kv:
            k, v = kv.split("=", 1)
            parts[k.strip()] = v.strip()
    ts = parts.get("t")
    sig = parts.get("v1")
    if not ts or not sig:
        return False, "malformed_signature"
    try:
        ts_int = int(ts)
    except ValueError:
        return False, "malformed_timestamp"
    if abs(time.time() - ts_int) > max_age:
        return False, "signature_expired"
    expected = sign_webhook(secret, payload, ts_int).split("v1=", 1)[1]
    if not hmac.compare_digest(expected, sig):
        return False, "signature_mismatch"
    return True, "ok"


def create_checkout(db, user_id, plan):
    """
    创建支付结算会话（模拟）。金额 = 套餐月价。

    Returns: (checkout, ok, reason)
    """
    if plan not in get_plans():
        return None, False, f"unknown_plan:{plan}"
    co = BillingCheckout(
        user_id=user_id,
        plan=plan,
        amount=get_price(plan),
        currency=get_currency(),
        status="open",
    )
    db.add(co)
    db.flush()
    return co, True, "ok"


def checkout_dict(co):
    return {
        "id": co.id,
        "plan": co.plan,
        "amount": co.amount,
        "currency": co.currency,
        "status": co.status,
        "created_at": _dt(co.created_at).isoformat() if co.created_at else None,
        "mock_pay_url": f"/api/v1/billing/mock-pay/{co.id}",
    }


def handle_webhook_event(db, event, commit=False):
    """
    处理 webhook 事件（模拟 Stripe 事件）：
    - payment_intent.succeeded → 标记 checkout paid + 激活订阅
    - subscription.updated → 同步订阅状态

    event: {"type": str, "data": {"checkout_id": int, "user_id": int, "plan": str, "subscription_status": str}}
    Returns: (ok, reason, detail)
    """
    ev_type = event.get("type", "")
    data = event.get("data", {}) or {}
    if ev_type == "payment_intent.succeeded":
        co = db.query(BillingCheckout).filter(BillingCheckout.id == int(data["checkout_id"])).first()
        if co is None:
            return False, "checkout_not_found", None
        if co.status == "paid":
            return True, "already_paid", None
        co.status = "paid"
        co.paid_at = now_utc()
        sub = get_subscription(db, co.user_id, lazy_expire=False)
        if sub is None:
            sub, ok, reason = create_subscription(db, co.user_id, co.plan)
            if not ok and sub is not None:
                # 已存在则直接激活
                activate_subscription(db, sub)
            elif sub is None:
                return False, "subscription_create_failed", None
        else:
            activate_subscription(db, sub)
        if commit:
            db.commit()
        return True, "ok", {"checkout_id": co.id, "subscription_id": sub.id, "plan": sub.plan, "status": sub.status}
    if ev_type == "subscription.updated":
        sub = db.query(Subscription).filter(Subscription.id == int(data["subscription_id"])).first()
        if sub is None:
            return False, "subscription_not_found", None
        new_status = data.get("subscription_status", "")
        if new_status and new_status in SUBSCRIPTION_STATUSES:
            transition(db, sub, new_status)
        if commit:
            db.commit()
        return True, "ok", {"subscription_id": sub.id, "status": sub.status}
    return False, f"unsupported_event:{ev_type}", None


# ─────────────────────────── 账单视图 ───────────────────────────

def build_invoice(db, user_id, sub=None):
    """
    当前周期账单视图：套餐费 + 超额用量费（overage）。
    返回 dict（不落库，按 config 实时计算）。
    """
    sub = sub or get_subscription(db, user_id)
    plan = sub.plan if sub else "free"
    if sub and sub.current_period_start and sub.current_period_end:
        start, end = _dt(sub.current_period_start), _dt(sub.current_period_end)
    else:
        start, end = _month_bounds()
    agg = aggregate_usage(db, user_id, plan=plan, period_start=start, period_end=end)
    base = get_price(plan)
    line_items = []
    total = base
    for item in agg["items"]:
        overage = item["over"] * get_overage_rate(item["event_type"])
        total += overage
        line_items.append({
            "event_type": item["event_type"],
            "used": item["used"],
            "quota": item["quota"],
            "over": item["over"],
            "overage_amount": round(overage, 2),
        })
    return {
        "period": {"start": start.isoformat(), "end": end.isoformat()},
        "plan": plan,
        "base_amount": round(base, 2),
        "currency": get_currency(),
        "line_items": line_items,
        "total_amount": round(total, 2),
    }


def list_paid_invoices(db, user_id):
    """历史账单：已支付 checkout 记录（作为历史账单列表）"""
    rows = (
        db.query(BillingCheckout)
        .filter(BillingCheckout.user_id == user_id, BillingCheckout.status == "paid")
        .order_by(BillingCheckout.paid_at.desc())
        .all()
    )
    return [
        {
            "checkout_id": c.id,
            "plan": c.plan,
            "amount": c.amount,
            "currency": c.currency,
            "paid_at": _dt(c.paid_at).isoformat() if c.paid_at else None,
        }
        for c in rows
    ]
