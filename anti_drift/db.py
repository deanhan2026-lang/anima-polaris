"""
anti_drift/db.py
SQLite 数据库初始化 — SQLAlchemy 2.0 style
"""

import os
from pathlib import Path
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base

REPO_ROOT = Path(__file__).parent.parent.resolve()
DB_PATH = REPO_ROOT / "data" / "polaris_saas.db"
DATABASE_URL = os.environ.get("POLARIS_DATABASE_URL", f"sqlite:///{DB_PATH}")

engine = create_engine(DATABASE_URL, echo=False, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def init_db():
    """创建所有表（幂等）"""
    Base.metadata.create_all(bind=engine)


def get_db():
    """FastAPI 风格依赖注入，也可独立使用"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
