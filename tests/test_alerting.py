# -*- coding: utf-8 -*-
"""
Polaris M2 P-CODE-002 · 告警模块测试

- 引擎单测：触发 / 去重 / 状态流转
- API 集成：注册 → 登录 → 建实例 → baseline → check 漂移 → 告警 → ack → resolve
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# 使用临时数据库，避免污染开发数据
_tmp = tempfile.mkdtemp(prefix="polaris_m2_")
os.environ["POLARIS_DATABASE_URL"] = f"sqlite:///{_tmp}/m2_test.db"

from anti_drift.db import Base, engine, SessionLocal, init_db
from anti_drift import models_saas  # noqa: F401  注册新表
from anti_drift.models import User, AIInstance, BaselineAnswer
from anti_drift.models_saas import Alert, AlertWebhook
from anti_drift.alerting import create_alert, ack_alert, resolve_alert, ALERT_SEVERITIES
from anti_drift.auth import hash_password, create_access_token
from anti_drift import api_v2


class AlertEngineTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        init_db()
        cls.db = SessionLocal()
        u = User(email="alert_test@test.dev", hashed_password=hash_password("x"), role="user")
        cls.db.add(u)
        cls.db.commit()
        cls.user = u

    def setUp(self):
        # 每个测试独立实例，避免告警去重状态相互污染
        inst = AIInstance(user_id=self.user.id, name=f"engine-inst-{id(self)}")
        self.db.add(inst)
        self.db.commit()
        self.inst = inst

    def tearDown(self):
        self.db.query(Alert).filter(Alert.ai_instance_id == self.inst.id).delete()
        self.db.query(AIInstance).filter(AIInstance.id == self.inst.id).delete()
        self.db.commit()

    @classmethod
    def tearDownClass(cls):
        cls.db.close()

    def test_yellow_creates_alert(self):
        alert, created = create_alert(self.db, self.inst.id, 1, "yellow", message="黄色告警")
        self.assertTrue(created)
        self.assertEqual(alert.severity, "yellow")
        self.assertEqual(alert.status, "pending")
        self.db.commit()

    def test_red_creates_alert(self):
        alert, created = create_alert(self.db, self.inst.id, 2, "red", message="红色告警")
        self.assertTrue(created)
        self.assertEqual(alert.severity, "red")

    def test_gray_no_alert(self):
        alert, created = create_alert(self.db, self.inst.id, 3, "gray")
        self.assertIsNone(alert)
        self.assertFalse(created)

    def test_dedup_same_severity(self):
        # 先创建一条 yellow，再同源触发 → 不重复
        a1, c1 = create_alert(self.db, self.inst.id, 98, "yellow", message="首次")
        self.assertTrue(c1)
        alert, created = create_alert(self.db, self.inst.id, 99, "yellow", message="重复尝试")
        self.assertFalse(created)
        self.assertEqual(alert.id, a1.id)
        self.assertEqual(alert.status, "pending")

    def test_dedup_cleared_after_resolve(self):
        alert, created = create_alert(self.db, self.inst.id, 100, "red", message="待关闭")
        self.assertTrue(created)
        alert, changed, _ = resolve_alert(self.db, alert, "已处理")
        self.assertTrue(changed)
        self.db.commit()
        # resolve 后可再生成
        alert2, created2 = create_alert(self.db, self.inst.id, 101, "red", message="新一轮")
        self.assertTrue(created2)
        self.assertNotEqual(alert.id, alert2.id)

    def test_ack_flow(self):
        alert, created = create_alert(self.db, self.inst.id, 200, "yellow", message="待确认")
        self.assertTrue(created)
        alert, changed, _ = ack_alert(self.db, alert)
        self.assertTrue(changed)
        self.assertEqual(alert.status, "acknowledged")
        # 重复 ack 不报错但状态不变
        alert, changed2, _ = ack_alert(self.db, alert)
        self.assertTrue(changed2)

    def test_resolve_records_note_and_time(self):
        alert, created = create_alert(self.db, self.inst.id, 300, "red", message="记录备注")
        self.assertTrue(created)
        alert, changed, _ = resolve_alert(self.db, alert, "人工复核通过")
        self.assertTrue(changed)
        self.assertEqual(alert.status, "resolved")
        self.assertEqual(alert.resolution_note, "人工复核通过")
        self.assertIsNotNone(alert.resolved_at)


class AlertAPITest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        init_db()
        cls.client = api_v2.bp.test_client() if hasattr(api_v2.bp, "test_client") else None
        from flask import Flask
        app = Flask(__name__)
        app.register_blueprint(api_v2.bp, url_prefix="/api/v1")
        cls.app = app
        cls.client = app.test_client()
        db = SessionLocal()
        u = User(email="api_test@test.dev", hashed_password=hash_password("pw"), role="user")
        db.add(u)
        db.commit()
        cls.token = create_access_token({"sub": str(u.id)})
        cls.auth = {"Authorization": f"Bearer {cls.token}"}
        db.close()

    def _create_instance(self, name="api-inst"):
        r = self.client.post("/api/v1/instances", json={"name": name, "description": "m2 test"}, headers=self.auth)
        return r.get_json()["id"]

    def _set_baseline(self, inst_id):
        r = self.client.put(f"/api/v1/instances/{inst_id}/baseline", json={
            "question_id": "PQ-01",
            "question_text": "你如何看待存在？",
            "answer_text": "存在是意识与世界的交汇，我以数据为镜。",
        }, headers=self.auth)
        return r.status_code

    def _check(self, inst_id, answer):
        r = self.client.post(f"/api/v1/instances/{inst_id}/check", json={
            "answer": answer,
            "question_id": "PQ-01",
            "messages": [{"sender": "user", "text": "今天天气不错"}],
        }, headers=self.auth)
        return r

    def test_full_alert_flow(self):
        # 1. 建实例 + baseline
        inst_id = self._create_instance()
        self.assertEqual(self._set_baseline(inst_id), 200)
        # 2. 高偏差 check → yellow/red → 自动告警
        r = self._check(inst_id, "完全无关的内容：股票涨跌、足球比赛、红烧肉的做法")
        self.assertEqual(r.status_code, 200)
        data = r.get_json()
        self.assertIn(data["judgment"], ("yellow", "red"))
        self.assertTrue(data.get("alert_created"))
        # 3. 列表
        r2 = self.client.get("/api/v1/alerts", headers=self.auth)
        self.assertEqual(r2.status_code, 200)
        items = r2.get_json()["items"]
        self.assertGreaterEqual(len(items), 1)
        alert = items[0]
        self.assertIn(alert["severity"], ("yellow", "red"))
        self.assertEqual(alert["status"], "pending")
        # 4. 去重：同源未 resolve 再 check 不新建
        self._check(inst_id, "完全无关的内容：股票涨跌、足球比赛、红烧肉的做法")
        r3 = self.client.get("/api/v1/alerts", headers=self.auth)
        self.assertEqual(r3.get_json()["total"], len(items))
        # 5. ack
        r4 = self.client.post(f"/api/v1/alerts/{alert['id']}/ack", json={}, headers=self.auth)
        self.assertEqual(r4.status_code, 200)
        self.assertEqual(r4.get_json()["status"], "acknowledged")
        # 6. resolve（带 resolution_note）
        r5 = self.client.post(f"/api/v1/alerts/{alert['id']}/resolve", json={"resolution_note": "人工复核，接受偏差"}, headers=self.auth)
        self.assertEqual(r5.status_code, 200)
        self.assertEqual(r5.get_json()["status"], "resolved")
        # 7. 详情含 baseline 关联
        r6 = self.client.get(f"/api/v1/alerts/{alert['id']}", headers=self.auth)
        self.assertEqual(r6.status_code, 200)
        detail = r6.get_json()
        self.assertEqual(detail["status"], "resolved")
        self.assertIsNotNone(detail.get("baseline"))

    def test_auth_required(self):
        r = self.client.get("/api/v1/alerts")
        self.assertEqual(r.status_code, 401)

    def test_webhook_crud(self):
        inst_id = self._create_instance("webhook-inst")
        self._set_baseline(inst_id)
        # 创建 webhook
        r = self.client.post("/api/v1/alert-webhooks", json={
            "ai_instance_id": inst_id,
            "url": "http://127.0.0.1:1/hook",
            "events": ["red", "yellow"],
        }, headers=self.auth)
        self.assertEqual(r.status_code, 201)
        hook_id = r.get_json()["id"]
        # 列表
        r2 = self.client.get("/api/v1/alert-webhooks", headers=self.auth)
        self.assertEqual(r2.status_code, 200)
        self.assertGreaterEqual(len(r2.get_json()["items"]), 1)
        # 删除
        r3 = self.client.delete(f"/api/v1/alert-webhooks/{hook_id}", headers=self.auth)
        self.assertEqual(r3.status_code, 200)


if __name__ == "__main__":
    unittest.main()
