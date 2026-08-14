"""
anti_drift/models.py
SQLAlchemy ORM 模型 — 纯数据层，不依赖 Flask
"""

from datetime import datetime, timezone
from sqlalchemy import Column, Integer, String, Text, Float, Boolean, DateTime, ForeignKey
from sqlalchemy.orm import relationship

from .db import Base


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, autoincrement=True)
    email = Column(String(256), unique=True, nullable=False, index=True)
    hashed_password = Column(String(256), nullable=False)
    role = Column(String(32), default="user")
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    ai_instances = relationship("AIInstance", back_populates="user", cascade="all, delete-orphan")

    def __repr__(self):
        # P-02 修复：User 无 username 字段，改用 email
        return f"<User(id={self.id}, email='{self.email}', role='{self.role}')>"


class AIInstance(Base):
    __tablename__ = "ai_instances"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    name = Column(String(128), nullable=False)
    description = Column(Text, default="")
    status = Column(String(16), default="active", index=True)  # active / archived
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    user = relationship("User", back_populates="ai_instances")
    baseline_answers = relationship("BaselineAnswer", back_populates="ai_instance", cascade="all, delete-orphan")
    drift_checks = relationship("DriftCheck", back_populates="ai_instance", cascade="all, delete-orphan")

    def __repr__(self):
        return f"<AIInstance(id={self.id}, name='{self.name}', user_id={self.user_id})>"


class BaselineAnswer(Base):
    __tablename__ = "baseline_answers"

    id = Column(Integer, primary_key=True, autoincrement=True)
    ai_instance_id = Column(Integer, ForeignKey("ai_instances.id"), nullable=False)
    question_id = Column(String(128), nullable=False, index=True)  # P-03 修复：64→128 防截断
    question_text = Column(Text, nullable=False)
    answer_text = Column(Text, nullable=False)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    ai_instance = relationship("AIInstance", back_populates="baseline_answers")
    drift_checks = relationship("DriftCheck", back_populates="baseline_answer")

    def __repr__(self):
        return f"<BaselineAnswer(id={self.id}, question_id='{self.question_id}')>"


class DriftCheck(Base):
    __tablename__ = "drift_checks"

    id = Column(Integer, primary_key=True, autoincrement=True)
    ai_instance_id = Column(Integer, ForeignKey("ai_instances.id"), nullable=False)
    baseline_answer_id = Column(Integer, ForeignKey("baseline_answers.id"), nullable=True)
    answer_text = Column(Text, nullable=False)
    deviation_score = Column(Float, default=0.0)
    dimension_scores = Column(Text, default="{}")
    judgment = Column(String(16), default="green")
    scene_tags = Column(Text, default="{}")
    checked_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    ai_instance = relationship("AIInstance", back_populates="drift_checks")
    baseline_answer = relationship("BaselineAnswer", back_populates="drift_checks")

    def __repr__(self):
        return f"<DriftCheck(id={self.id}, judgment='{self.judgment}', score={self.deviation_score})>"
