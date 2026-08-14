# -*- coding: utf-8 -*-
"""
anti_drift/alerting.py
Polaris M2 · 告警触发引擎（TK P-CODE-002）

职责：
- 漂移检测结果 severity ∈ {yellow, red} → 自动写入 alerts 表
- 去重：同实例 + 同 severity 未 resolve 不重复生成
- Webhook 通知（v0.2.1 接口先行）：告警生成时按 alert_webhooks 配置 POST，失败静默不阻塞

联动点：api_v2.check_drift 提交检测后调用 create_alert()
"""
import json
import logging
from datetime import datetime, timezone
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

from .models_saas import Alert, AlertWebhook

logger = logging.getLogger("anti_drift.alerting")

# 需要告警的判定级别
ALERT_SEVERITIES = {"yellow", "red"}


def create_alert(db, ai_instance_id, drift_check_id, severity, message=None, fire_webhook=True):
    """
    生成告警（幂等去重）。

    Args:
        db: SQLAlchemy session
        ai_instance_id: 实例 ID
        drift_check_id: 关联漂移检测 ID
        severity: gray / yellow / red
        message: 告警摘要（可选）
        fire_webhook: 是否触发 webhook 推送（默认 True）

    Returns:
        (alert_or_None, created: bool)
        - created=True: 新建
        - created=False: 已存在未 resolve 的同源告警（不重复）
        - created=False 且 alert=None: severity 不在告警范围
    """
    if severity not in ALERT_SEVERITIES:
        return None, False

    # 去重：同实例 + 同 severity + 未 resolve
    existing = (
        db.query(Alert)
        .filter(
            Alert.ai_instance_id == ai_instance_id,
            Alert.severity == severity,
            Alert.status != "resolved",
        )
        .first()
    )
    if existing:
        return existing, False

    alert = Alert(
        ai_instance_id=ai_instance_id,
        drift_check_id=drift_check_id,
        severity=severity,
        status="pending",
        message=message or f"漂移检测触发 {severity} 告警",
    )
    db.add(alert)
    db.flush()  # 取 id 但不 commit（由调用方统一 commit）

    if fire_webhook:
        try:
            fire_webhooks(db, ai_instance_id, alert)
        except Exception as e:  # webhook 失败静默，不阻塞主流程
            logger.warning("webhook 推送失败（静默）: %s", e)

    return alert, True


def fire_webhooks(db, ai_instance_id, alert):
    """按配置推送 webhook（失败静默，不抛出）"""
    hooks = (
        db.query(AlertWebhook)
        .filter(
            AlertWebhook.ai_instance_id == ai_instance_id,
            AlertWebhook.enabled == True,  # noqa: E712
        )
        .all()
    )
    for hook in hooks:
        try:
            events = json.loads(hook.events or '["red","yellow"]')
        except Exception:
            events = ["red", "yellow"]
        if alert.severity not in events:
            continue
        payload = json.dumps({
            "type": "alert",
            "id": alert.id,
            "severity": alert.severity,
            "status": alert.status,
            "message": alert.message,
            "ai_instance_id": alert.ai_instance_id,
            "drift_check_id": alert.drift_check_id,
            "created_at": alert.created_at.isoformat() if alert.created_at else None,
        }).encode("utf-8")
        req = Request(
            hook.url,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urlopen(req, timeout=5) as resp:
                logger.info("webhook %s -> HTTP %s", hook.url, resp.status)
        except (URLError, HTTPError, OSError) as e:
            logger.warning("webhook %s 失败: %s（静默）", hook.url, e)


def ack_alert(db, alert, user_id=None):
    """人工确认：pending/acknowledged -> acknowledged"""
    if alert.status == "resolved":
        return alert, False, "已关闭的告警不可再确认"
    alert.status = "acknowledged"
    return alert, True, "ok"


def resolve_alert(db, alert, resolution_note=None):
    """处理关闭：-> resolved（记录 resolution_note + resolved_at）"""
    if alert.status == "resolved":
        return alert, False, "已关闭"
    alert.status = "resolved"
    alert.resolution_note = (resolution_note or "").strip()
    alert.resolved_at = datetime.now(timezone.utc)
    return alert, True, "ok"
