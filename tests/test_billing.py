# -*- coding: utf-8 -*-
"""
Polaris M3 P-CODE-003 订阅计费模块测试（v0.3）

覆盖验收六项：
1. 订阅状态机（trial / active / canceled / expired + 非法迁移防护）
2. 用量事件记录 + 月周期聚合（月对齐 + over_limit flag）
3. 支付 checkout + webhook 回调闭环（模拟签名可验证）
4. 控制台订阅 API（套餐 / 用量 / 账单 / 升级降级）
5. pytest 全绿（本文件即新增用例）
6. tag v0.3.0（交付流程另行执行）
"""
import json
import os
import sys
import tempfile
import time
import unittest
import importlib
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# 临时数据库，避免污染开发数据
_tmp = tempfile.mkdtemp(prefix="polaris_m3_")
os.environ["POLARIS_DATABASE_URL"] = f"sqlite:///{_tmp}/m3_test.db"

from flask import Flask

from anti_drift.db import Base, engine, SessionLocal, init_db
from anti_drift import models_saas  # noqa: F401 注册新表
from anti_drift.models import User
from anti_drift.models_saas import Subscription, UsageEvent, BillingCheckout
from anti_drift.auth import hash_password, create_access_token
from anti_drift import billing, billing_api, api_v2


class BillingEngineTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        init_db()
        cls.db = SessionLocal()
        u = User(email="billing_engine@test.dev", hashed_password=hash_password("x"), role="user")
        cls.db.add(u)
        cls.db.commit()
        cls.user = u

    def tearDown(self):
        # 清理本测试产生的订阅/用量/结算，避免类间污染
        self.db.query(UsageEvent).filter(UsageEvent.user_id == self.user.id).delete()
        self.db.query(BillingCheckout).filter(BillingCheckout.user_id == self.user.id).delete()
        self.db.query(Subscription).filter(Subscription.user_id == self.user.id).delete()
        self.db.commit()

    @classmethod
    def tearDownClass(cls):
        cls.db.close()

    # ── 验收 1：状态机 ──
    def test_create_subscription_starts_trial(self):
        sub, ok, reason = billing.create_subscription(self.db, self.user.id, "pro")
        self.assertTrue(ok)
        self.assertEqual(sub.status, "trial")
        self.assertEqual(sub.plan, "pro")
        self.assertEqual(sub.current_period_end - sub.current_period_start, timedelta(days=14))
        self.db.commit()

    def test_trial_to_active_to_canceled_to_expired(self):
        sub, _, _ = billing.create_subscription(self.db, self.user.id, "pro")
        # trial -> active（支付成功）
        sub, ok, _ = billing.activate_subscription(self.db, sub)
        self.assertTrue(ok)
        self.assertEqual(sub.status, "active")
        # active -> canceled
        sub, ok, _ = billing.cancel_subscription(self.db, sub)
        self.assertTrue(ok)
        self.assertEqual(sub.status, "canceled")
        # canceled -> expired（周期结束）
        sub.current_period_end = datetime.now(timezone.utc) - timedelta(seconds=1)
        sub2, changed = billing.expire_if_due(self.db, sub)
        self.assertTrue(changed)
        self.assertEqual(sub2.status, "expired")
        # expired -> active（重新订阅）
        sub2, ok, reason = billing.activate_subscription(self.db, sub2)
        self.assertTrue(ok)
        self.assertEqual(sub2.status, "active")
        self.db.commit()

    def test_illegal_transitions_rejected(self):
        sub, _, _ = billing.create_subscription(self.db, self.user.id, "free")
        # trial -> trial 原地（already）
        sub, ok, reason = billing.transition(self.db, sub, "trial")
        self.assertFalse(ok)
        self.assertEqual(reason, "already_trial")
        # trial -> active 合法，active -> trial 非法
        sub, ok, _ = billing.activate_subscription(self.db, sub)
        self.assertTrue(ok)
        self.assertEqual(sub.status, "active")
        sub, ok, reason = billing.transition(self.db, sub, "trial")
        self.assertFalse(ok)
        self.assertIn("illegal_transition", reason)
        self.assertEqual(sub.status, "active")
        # active -> canceled 合法，canceled -> trial 非法
        sub, ok, _ = billing.cancel_subscription(self.db, sub)
        self.assertTrue(ok)
        sub, ok, reason = billing.transition(self.db, sub, "trial")
        self.assertFalse(ok)
        self.assertIn("illegal_transition", reason)
        self.db.commit()

    def test_duplicate_active_subscription_rejected(self):
        billing.create_subscription(self.db, self.user.id, "free")
        self.db.commit()
        sub, ok, reason = billing.create_subscription(self.db, self.user.id, "pro")
        self.assertFalse(ok)
        self.assertEqual(reason, "subscription_already_trial")
        self.db.commit()

    def test_lazy_expire_on_get(self):
        sub, _, _ = billing.create_subscription(self.db, self.user.id, "free")
        sub.current_period_end = datetime.now(timezone.utc) - timedelta(hours=1)
        self.db.commit()
        got = billing.get_subscription(self.db, self.user.id)
        self.assertEqual(got.status, "expired")
        self.db.commit()

    # ── 验收 2：用量记录 + 周期聚合 ──
    def test_record_usage_basic(self):
        ev, ok, reason = billing.record_usage(self.db, self.user.id, "detection", 3)
        self.assertTrue(ok)
        self.assertEqual(ev.quantity, 3)
        self.db.commit()

    def test_record_usage_invalid(self):
        ev, ok, reason = billing.record_usage(self.db, self.user.id, "nonsense")
        self.assertFalse(ok)
        ev, ok, reason = billing.record_usage(self.db, self.user.id, "detection", 0)
        self.assertFalse(ok)
        self.assertEqual(reason, "invalid_quantity")

    def test_monthly_aggregation_over_limit(self):
        # free 套餐 detection 限额 100：记录 105 → over_limit
        billing.create_subscription(self.db, self.user.id, "free")
        for _ in range(105):
            billing.record_usage(self.db, self.user.id, "detection", 1)
        self.db.commit()
        agg = billing.aggregate_usage(self.db, self.user.id, plan="free")
        detection = next(i for i in agg["items"] if i["event_type"] == "detection")
        self.assertEqual(detection["used"], 105)
        self.assertEqual(detection["quota"], 100)
        self.assertTrue(detection["over_limit"])
        self.assertTrue(agg["overall_over_limit"])
        # 月周期对齐：period.start 为当月 1 号 00:00:00 UTC
        now = datetime.now(timezone.utc)
        start = datetime(now.year, now.month, 1, tzinfo=timezone.utc)
        self.assertEqual(agg["period"]["start"], start.isoformat())

    def test_aggregation_within_quota(self):
        billing.create_subscription(self.db, self.user.id, "free")
        billing.record_usage(self.db, self.user.id, "detection", 50)
        billing.record_usage(self.db, self.user.id, "snapshot", 2)
        self.db.commit()
        agg = billing.aggregate_usage(self.db, self.user.id, plan="free")
        self.assertFalse(agg["overall_over_limit"])
        items = {i["event_type"]: i for i in agg["items"]}
        self.assertEqual(items["detection"]["used"], 50)
        self.assertEqual(items["snapshot"]["used"], 2)
        self.assertEqual(items["alert_push"]["used"], 0)

    # ── 验收 3：checkout + webhook 签名闭环 ──
    def test_checkout_created(self):
        co, ok, _ = billing.create_checkout(self.db, self.user.id, "pro")
        self.assertTrue(ok)
        self.assertEqual(co.status, "open")
        self.assertEqual(co.amount, 49.0)
        self.db.commit()

    def test_webhook_signature_verify(self):
        secret = billing.get_webhook_secret()
        payload = json.dumps({"type": "payment_intent.succeeded", "data": {"checkout_id": 1}}).encode("utf-8")
        sig = billing.sign_webhook(secret, payload)
        ok, _ = billing.verify_webhook(secret, payload, sig)
        self.assertTrue(ok)
        # 篡改 payload → 拒绝
        tampered = payload + b"x"
        ok, reason = billing.verify_webhook(secret, tampered, sig)
        self.assertFalse(ok)
        self.assertEqual(reason, "signature_mismatch")
        # 过期时间戳 → 拒绝
        old_sig = billing.sign_webhook(secret, payload, timestamp=int(time.time()) - 10000)
        ok, reason = billing.verify_webhook(secret, payload, old_sig)
        self.assertFalse(ok)
        self.assertEqual(reason, "signature_expired")
        # 缺失 header → 拒绝
        ok, reason = billing.verify_webhook(secret, payload, "")
        self.assertFalse(ok)
        self.assertEqual(reason, "missing_signature")

    def test_mock_pay_activates_subscription(self):
        billing.create_subscription(self.db, self.user.id, "pro")  # trial
        self.db.commit()
        co, _, _ = billing.create_checkout(self.db, self.user.id, "pro")
        event = {
            "type": "payment_intent.succeeded",
            "data": {"checkout_id": co.id, "user_id": self.user.id, "plan": "pro"},
        }
        ok, reason, detail = billing.handle_webhook_event(self.db, event, commit=True)
        self.assertTrue(ok)
        self.assertEqual(detail["status"], "active")
        co2 = self.db.get(BillingCheckout, co.id)
        self.assertEqual(co2.status, "paid")
        sub = billing.get_subscription(self.db, self.user.id)
        self.assertEqual(sub.status, "active")

    # ── 验收 4（引擎侧）：升级 / 降级 ──
    def test_change_plan_upgrade_downgrade(self):
        sub, _, _ = billing.create_subscription(self.db, self.user.id, "free")
        sub, ok, _ = billing.change_plan(self.db, sub, "pro")
        self.assertTrue(ok)
        self.assertEqual(sub.plan, "pro")
        sub, ok, _ = billing.change_plan(self.db, sub, "enterprise")
        self.assertTrue(ok)
        self.assertEqual(sub.plan, "enterprise")
        sub, ok, _ = billing.change_plan(self.db, sub, "free")
        self.assertTrue(ok)
        self.assertEqual(sub.plan, "free")
        # 相同套餐 → 拒绝
        sub, ok, reason = billing.change_plan(self.db, sub, "free")
        self.assertFalse(ok)
        self.assertEqual(reason, "plan_unchanged")
        self.db.commit()

    def test_change_plan_rejected_when_canceled(self):
        sub, _, _ = billing.create_subscription(self.db, self.user.id, "free")
        billing.cancel_subscription(self.db, sub)
        sub, ok, reason = billing.change_plan(self.db, sub, "pro")
        self.assertFalse(ok)
        self.assertIn("cannot_change_plan", reason)
        self.db.commit()

    # ── 验收 4（引擎侧）：账单视图 ──
    def test_invoice_view(self):
        billing.create_subscription(self.db, self.user.id, "pro")
        for _ in range(120):  # 超 detection 限额 5000? 不超；用 5050 超
            pass
        # 构造：pro 套餐 + 用量未超限 → 仅基础费
        for _ in range(10):
            billing.record_usage(self.db, self.user.id, "detection", 1)
        self.db.commit()
        inv = billing.build_invoice(self.db, self.user.id)
        self.assertEqual(inv["plan"], "pro")
        self.assertEqual(inv["base_amount"], 49.0)
        self.assertEqual(inv["total_amount"], 49.0)
        # 超额计费：free 套餐 detection 超 10 次 → overage 0.1
        self.db.query(Subscription).filter(Subscription.user_id == self.user.id).delete()
        self.db.commit()
        billing.create_subscription(self.db, self.user.id, "free")
        for _ in range(110):
            billing.record_usage(self.db, self.user.id, "detection", 1)
        self.db.commit()
        inv2 = billing.build_invoice(self.db, self.user.id)
        detection_line = next(i for i in inv2["line_items"] if i["event_type"] == "detection")
        self.assertEqual(detection_line["over"], 10)
        self.assertGreater(inv2["total_amount"], 0)

    # ── 验收 5（配置）：config.yaml 为限额权威来源 ──
    def test_config_authoritative(self):
        self.assertEqual(billing.get_price("free"), 0)
        self.assertEqual(billing.get_price("pro"), 49)
        self.assertEqual(billing.get_price("enterprise"), 199)
        self.assertEqual(billing.get_quota("free", "detection"), 100)
        self.assertEqual(billing.get_trial_days(), 14)
        self.assertTrue(billing.get_webhook_secret())

    def test_config_env_override(self):
        # 环境变量 POLARIS_BILLING_* 可逐项覆盖（config.py 最高优先级，无需 reload）
        import anti_drift.config as cfg_mod
        os.environ["POLARIS_BILLING_PLANS_FREE_QUOTA_DETECTION"] = "42"
        os.environ["POLARIS_BILLING_TRIAL_DAYS"] = "30"
        try:
            importlib.reload(cfg_mod)
            self.assertEqual(billing.get_quota("free", "detection"), "42")
            self.assertEqual(billing.get_trial_days(), 30)
        finally:
            del os.environ["POLARIS_BILLING_PLANS_FREE_QUOTA_DETECTION"]
            del os.environ["POLARIS_BILLING_TRIAL_DAYS"]
            importlib.reload(cfg_mod)
        self.assertEqual(billing.get_quota("free", "detection"), 100)
        self.assertEqual(billing.get_trial_days(), 14)


class BillingAPITest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        init_db()
        app = Flask(__name__)
        app.register_blueprint(api_v2.bp, url_prefix="/api/v1")
        app.register_blueprint(billing_api.bp, url_prefix="/api/v1/billing")
        cls.app = app
        cls.client = app.test_client()
        # 通过 API 注册 + 登录获取 token
        r = cls.client.post("/api/v1/auth/register", json={"email": "billing_api@test.dev", "password": "pw"})
        assert r.status_code == 201, r.get_json()
        r = cls.client.post("/api/v1/auth/login", json={"email": "billing_api@test.dev", "password": "pw"})
        cls.token = r.get_json()["access_token"]
        cls.auth = {"Authorization": f"Bearer {cls.token}"}

    def tearDown(self):
        db = SessionLocal()
        try:
            u = db.query(User).filter(User.email == "billing_api@test.dev").first()
            if u:
                db.query(UsageEvent).filter(UsageEvent.user_id == u.id).delete()
                db.query(BillingCheckout).filter(BillingCheckout.user_id == u.id).delete()
                db.query(Subscription).filter(Subscription.user_id == u.id).delete()
                db.commit()
        finally:
            db.close()

    def test_auth_required(self):
        r = self.client.get("/api/v1/billing/subscription")
        self.assertEqual(r.status_code, 401)

    def test_full_billing_flow(self):
        # 1. 套餐列表
        r = self.client.get("/api/v1/billing/plans", headers=self.auth)
        self.assertEqual(r.status_code, 200)
        plans = r.get_json()["plans"]
        self.assertIn("pro", plans)
        self.assertEqual(plans["pro"]["price_monthly"], 49)
        # 2. 创建订阅 → trial
        r = self.client.post("/api/v1/billing/subscription", json={"plan": "pro"}, headers=self.auth)
        self.assertEqual(r.status_code, 201)
        sub = r.get_json()["subscription"]
        self.assertEqual(sub["status"], "trial")
        self.assertEqual(sub["plan"], "pro")
        # 3. 记录用量 + 汇总
        for _ in range(5):
            self.client.post("/api/v1/billing/usage", json={"event_type": "detection", "quantity": 1}, headers=self.auth)
        r = self.client.get("/api/v1/billing/usage/summary", headers=self.auth)
        self.assertEqual(r.status_code, 200)
        summary = r.get_json()
        detection = next(i for i in summary["items"] if i["event_type"] == "detection")
        self.assertEqual(detection["used"], 5)
        self.assertFalse(detection["over_limit"])
        # 4. checkout → 模拟支付 → active
        r = self.client.post("/api/v1/billing/checkout", json={"plan": "pro"}, headers=self.auth)
        self.assertEqual(r.status_code, 201)
        checkout = r.get_json()["checkout"]
        self.assertEqual(checkout["status"], "open")
        r = self.client.post(f"/api/v1/billing/mock-pay/{checkout['id']}", headers=self.auth)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["detail"]["status"], "active")
        # 5. 订阅状态确认
        r = self.client.get("/api/v1/billing/subscription", headers=self.auth)
        self.assertEqual(r.get_json()["subscription"]["status"], "active")
        # 6. 升级 pro -> enterprise
        r = self.client.post("/api/v1/billing/subscription/change", json={"plan": "enterprise"}, headers=self.auth)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["subscription"]["plan"], "enterprise")
        # 7. 降级 enterprise -> pro
        r = self.client.post("/api/v1/billing/subscription/change", json={"plan": "pro"}, headers=self.auth)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["subscription"]["plan"], "pro")
        # 8. 账单视图（当前周期 + 历史）
        r = self.client.get("/api/v1/billing/invoices", headers=self.auth)
        self.assertEqual(r.status_code, 200)
        inv = r.get_json()
        self.assertEqual(inv["current"]["plan"], "pro")
        self.assertGreaterEqual(inv["current"]["total_amount"], 49)
        self.assertEqual(len(inv["history"]), 1)  # 一次已支付 checkout
        self.assertEqual(inv["history"][0]["plan"], "pro")
        # 9. 取消订阅
        r = self.client.post("/api/v1/billing/subscription/cancel", headers=self.auth)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["subscription"]["status"], "canceled")

    def test_webhook_endpoint_signature(self):
        # 无签名 → 400
        r = self.client.post("/api/v1/billing/webhook", json={"type": "payment_intent.succeeded", "data": {}})
        self.assertEqual(r.status_code, 400)
        self.assertIn("missing_signature", r.get_json()["error"])
        # 正确签名 → 处理（checkout 不存在 → 422）
        payload = json.dumps({"type": "payment_intent.succeeded", "data": {"checkout_id": 99999}}).encode("utf-8")
        sig = billing.sign_webhook(billing.get_webhook_secret(), payload)
        r = self.client.post(
            "/api/v1/billing/webhook",
            data=payload,
            content_type="application/json",
            headers={"X-Polaris-Signature": sig},
        )
        self.assertEqual(r.status_code, 422)

    def test_webhook_roundtrip_via_signature(self):
        # 完整闭环：API 创建订阅 + checkout → 外部模拟支付走 webhook 端点（签名验证）→ active
        self.client.post("/api/v1/billing/subscription", json={"plan": "pro"}, headers=self.auth)
        r = self.client.post("/api/v1/billing/checkout", json={"plan": "pro"}, headers=self.auth)
        co_id = r.get_json()["checkout"]["id"]
        payload = json.dumps({
            "type": "payment_intent.succeeded",
            "data": {"checkout_id": co_id, "user_id": None, "plan": "pro"},
        }).encode("utf-8")
        sig = billing.sign_webhook(billing.get_webhook_secret(), payload)
        r = self.client.post(
            "/api/v1/billing/webhook",
            data=payload,
            content_type="application/json",
            headers={"X-Polaris-Signature": sig},
        )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["detail"]["status"], "active")
        r = self.client.get("/api/v1/billing/subscription", headers=self.auth)
        self.assertEqual(r.get_json()["subscription"]["status"], "active")


if __name__ == "__main__":
    unittest.main()
