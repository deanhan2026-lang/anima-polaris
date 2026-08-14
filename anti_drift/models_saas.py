# -*- coding: utf-8 -*-
"""
anti_drift/models_saas.py
Polaris M1 · SaaS 化新增 6 表（对应 M1_db_schema_v1_20260814.md）

新增表：
- snapshots        v0.1 人格快照（SHA256 溯源）
- audit_logs       v0.1 审计日志（合规导出，只追加不更新）
- alerts           v0.2 告警记录
- alert_webhooks   v0.2 告警推送配置
- subscriptions    v0.3 订阅计划
- usage_events     v0.3 用量计费事件

存量 4 表（users/ai_instances/baseline_answers/drift_checks）保持不动，见 models.py。
注册方式：Base.metadata.create_all() 幂等建表（db.py init_db），本模块被 import 即注册。
"""
from datetime import datetime, timezone

from sqlalchemy import Column, Integer, String, Text, Float, Boolean, DateTime, ForeignKey

from .db import Base


class Snapshot(Base):
    """人格快照 · SHA256 溯源（v0.1）"""
    __tablename__ = "snapshots"

    id = Column(Integer, primary_key=True, autoincrement=True)
    ai_instance_id = Column(Integer, ForeignKey("ai_instances.id"), nullable=False, index=True)
    sha256 = Column(String(64), nullable=False, index=True)
    snapshot_json = Column(Text, nullable=False)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))


class AuditLog(Base):
    """审计日志 · 合规导出（v0.1，只追加不更新）"""
    __tablename__ = "audit_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    action = Column(String(64), nullable=False)
    target_type = Column(String(32))
    target_id = Column(Integer)
    detail = Column(Text, default="{}")
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), index=True)


class Alert(Base):
    """告警记录（v0.2）"""
    __tablename__ = "alerts"

    id = Column(Integer, primary_key=True, autoincrement=True)
    ai_instance_id = Column(Integer, ForeignKey("ai_instances.id"), nullable=False, index=True)
    drift_check_id = Column(Integer, ForeignKey("drift_checks.id"), nullable=True)
    severity = Column(String(16), nullable=False, index=True)   # gray / yellow / red
    status = Column(String(16), default="pending", index=True)  # pending / acknowledged / resolved
    message = Column(Text)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    resolved_at = Column(DateTime, nullable=True)


class AlertWebhook(Base):
    """告警推送配置（v0.2）"""
    __tablename__ = "alert_webhooks"

    id = Column(Integer, primary_key=True, autoincrement=True)
    ai_instance_id = Column(Integer, ForeignKey("ai_instances.id"), nullable=False, index=True)
    url = Column(String(512), nullable=False)
    events = Column(Text, default='["red","yellow"]')  # JSON 数组
    enabled = Column(Boolean, default=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))


class Subscription(Base):
    """订阅计划（v0.3）"""
    __tablename__ = "subscriptions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    plan = Column(String(16), default="free", index=True)  # free / pro / enterprise
    status = Column(String(16), default="active")          # active / canceled / past_due
    current_period_start = Column(DateTime)
    current_period_end = Column(DateTime)
    stripe_customer_id = Column(String(128))
    stripe_subscription_id = Column(String(128))
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))


class UsageEvent(Base):
    """用量计费事件（v0.3）"""
    __tablename__ = "usage_events"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    ai_instance_id = Column(Integer, ForeignKey("ai_instances.id"), nullable=True)
    event_type = Column(String(32), nullable=False)  # detection / snapshot / alert_push
    quantity = Column(Integer, default=1)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), index=True)
