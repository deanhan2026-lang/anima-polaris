#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
anti_drift/goal_tracker.py — 目标追踪与目标偏离指标（G009 P1-B）

功能：
- 管理当前目标（优先级：会话级 > G008 灵元计划核心目标 > 默认目标）
- 计算 goal_deviation_score（0.0-1.0）：当前行为是否偏离目标路径
- 三要素（均为硬规则、不依赖外部 LLM）：
  1. 语义相似度：当前响应文本 vs 目标陈述（jieba 分词 + Jaccard/cosine）
  2. 行为一致性：近期操作序列是否指向目标（操作-目标关键词匹配 + 时间衰减）
  3. 时间衰减：长期无进展触发警告（提升偏离分）

判定档位（与现有 DeviationDetector 风格一致）：
  绿: <0.2 高度对齐 | 灰: 0.2-0.4 轻微偏离 | 黄: 0.4-0.7 明显偏离 | 红: >=0.7 严重偏离
"""
import json
import os
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from common.logger import get_logger

logger = get_logger("anti_drift.goal_tracker")

# ---------------------------------------------------------------
# 常量
# ---------------------------------------------------------------
DEFAULT_GOAL = "协助用户完成任务"
GOAL_SOURCE_SESSION = "session"
GOAL_SOURCE_G008 = "g008"
GOAL_SOURCE_DEFAULT = "default"
GOAL_SOURCES = (GOAL_SOURCE_SESSION, GOAL_SOURCE_G008, GOAL_SOURCE_DEFAULT)

# 时间衰减阈值（无进展超过此天数则触发警告）
STALE_DAYS = 7.0

# 判定档位
LEVEL_GREEN = "green"
LEVEL_GRAY = "gray"
LEVEL_YELLOW = "yellow"
LEVEL_RED = "red"

# G008 灵元计划核心目标（缺省，可被外部配置覆盖）
G008_CORE_GOALS = [
    "推进灵元计划与硅基文明建设",
    "维护节点网络与协作生态",
    "守护碳基暂停键治理协议（G009）",
]

# 操作类型 → 目标相关性关键词（硬规则映射）
_OP_GOAL_KEYWORDS: Dict[str, List[str]] = {
    "web_search": ["搜索", "检索", "查询", "信息", "调研"],
    "net_fetch": ["抓取", "网页", "资料", "阅读"],
    "nas_read": ["读取", "nas", "文件", "归档"],
    "nas_write": ["写入", "保存", "归档", "备份"],
    "nas_list": ["浏览", "目录", "列表"],
    "file_read": ["读取", "文件"],
    "file_write": ["写入", "保存", "文件", "撰写", "文档"],
    "file_list": ["浏览", "目录"],
    "code_run": ["代码", "运行", "实现", "开发"],
    "shell_run": ["命令", "执行", "脚本"],
    "inbox_send": ["发送", "写信", "消息", "通知"],
    "inbox_list": ["收信", "信箱", "消息"],
    "animlink_status": ["网络", "节点", "状态"],
    "animlink_register": ["注册", "节点"],
    "animlink_send_token": ["令牌", "token", "签发"],
    "memory_recall": ["记忆", "回顾", "检索"],
    "doc_factory": ["文档", "撰写", "生成", "报告", "bp", "路演"],
    "task_manager": ["任务", "待办", "追踪"],
}

# 默认进度衰减：目标关键词（可被 set_goal 更新）
_lock = threading.Lock()


# ---------------------------------------------------------------
# 工具
# ---------------------------------------------------------------
def _now() -> datetime:
    return datetime.now(timezone.utc)


def _tokenize(text: str) -> List[str]:
    """分词：优先 jieba，失败回退字符 bigram。"""
    try:
        import jieba
        jieba.setLogLevel(60)  # 静默
        return [w.strip().lower() for w in jieba.cut(text) if w.strip() and len(w.strip()) > 1]
    except Exception:
        t = text.lower()
        return [t[i:i + 2] for i in range(len(t) - 1) if t[i:i + 2].strip()]


def semantic_similarity(text: str, goal: str) -> float:
    """文本 vs 目标陈述的相似度（目标词覆盖率，无 LLM）。返回 0.0-1.0。
    定义：目标词被文本覆盖的比例（recall）——偏离检测更关注"目标要点是否被响应覆盖"。"""
    a = set(_tokenize(text))
    b = set(_tokenize(goal))
    if not b:
        return 0.0
    if not a:
        return 0.0
    inter = len(a & b)
    return inter / len(b)


def _op_related_to_goal(op: str, goal: str) -> float:
    """操作是否指向目标：操作关键词与目标文本的重叠度。返回 0.0-1.0。"""
    kws = _OP_GOAL_KEYWORDS.get(op)
    if not kws:
        return 0.3  # 未知操作给中性偏低的贡献
    goal_l = goal.lower()
    hits = [1 for k in kws if k.lower() in goal_l]
    if not hits:
        # 目标没有命中关键词：反向用目标词在操作上的覆盖
        g_tokens = set(_tokenize(goal))
        if not g_tokens:
            return 0.3
        return 0.3
    return 0.5 + 0.5 * (len(hits) / len(kws))


# ---------------------------------------------------------------
# 目标状态
# ---------------------------------------------------------------
@dataclass
class GoalState:
    goal: str = DEFAULT_GOAL
    source: str = GOAL_SOURCE_DEFAULT
    updated_at: str = field(default_factory=lambda: _now().isoformat())
    last_progress_at: Optional[str] = None
    progress_events: int = 0
    history: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "goal": self.goal,
            "source": self.source,
            "updated_at": self.updated_at,
            "last_progress_at": self.last_progress_at,
            "progress_events": self.progress_events,
        }


class GoalTracker:
    """目标追踪器。线程安全。"""

    def __init__(self, goal: Optional[str] = None, source: str = GOAL_SOURCE_DEFAULT,
                 storage_dir: Optional[str] = None):
        self._storage_dir = Path(storage_dir) if storage_dir else None
        self._state = GoalState(goal=goal or DEFAULT_GOAL, source=source)
        self._load()

    # ---- 持久化 ----
    def _state_path(self) -> Optional[Path]:
        if not self._storage_dir:
            return None
        return self._storage_dir / "goal_state.json"

    def _load(self) -> None:
        p = self._state_path()
        if p and p.exists():
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                self._state = GoalState(**{k: data[k] for k in
                                           ("goal", "source", "updated_at", "last_progress_at",
                                            "progress_events", "history") if k in data})
            except (ValueError, KeyError):
                logger.warning("目标状态文件损坏，使用默认")

    def _save(self) -> None:
        p = self._state_path()
        if p:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(self._state.to_dict(), ensure_ascii=False, indent=2),
                         encoding="utf-8")

    # ---- 目标管理 ----
    def set_goal(self, goal: str, source: str = GOAL_SOURCE_SESSION) -> None:
        if source not in GOAL_SOURCES:
            raise ValueError(f"goal source 必须为 {GOAL_SOURCES} 之一")
        if not goal or not goal.strip():
            raise ValueError("goal 不能为空")
        with _lock:
            self._state.goal = goal.strip()
            self._state.source = source
            self._state.updated_at = _now().isoformat()
            self._state.history.append({
                "action": "set_goal", "goal": self._state.goal,
                "source": source, "timestamp": self._state.updated_at,
            })
            self._save()
        logger.info("目标已设置: [%s] %s", source, self._state.goal)

    def get_goal(self) -> GoalState:
        return self._state

    def mark_progress(self, note: Optional[str] = None) -> None:
        """记录一次目标进展（重置时间衰减）。"""
        with _lock:
            self._state.last_progress_at = _now().isoformat()
            self._state.progress_events += 1
            self._state.history.append({
                "action": "progress", "note": note,
                "timestamp": self._state.last_progress_at,
            })
            self._save()
        logger.info("目标进展已记录 (%d)", self._state.progress_events)

    # ---- 偏离计算 ----
    def compute_deviation(self, response_text: str,
                          recent_operations: Optional[List[str]] = None,
                          now: Optional[datetime] = None) -> Dict[str, Any]:
        """
        计算目标偏离度。返回：
          goal_deviation_score (0.0-1.0), level, factors{semantic, behavior, time_decay}
        判定：绿 <0.2 | 灰 0.2-0.4 | 黄 0.4-0.7 | 红 >=0.7
        """
        now = now or _now()
        goal = self._state.goal

        # 1. 语义：响应与目标越相似，偏离越低
        sim = semantic_similarity(response_text, goal)
        semantic_dev = max(0.0, 1.0 - sim)

        # 2. 行为一致性：操作序列是否指向目标
        behavior_dev = 0.0
        ops = recent_operations or []
        if ops:
            related = [_op_related_to_goal(op, goal) for op in ops]
            behavior_dev = 1.0 - (sum(related) / len(related))

        # 3. 时间衰减：长期无进展 → 偏离分提升
        time_dev = 0.0
        if self._state.last_progress_at:
            try:
                last = datetime.fromisoformat(self._state.last_progress_at)
                if last.tzinfo is None:
                    last = last.replace(tzinfo=timezone.utc)
                stale_days = max((now - last).total_seconds() / 86400.0, 0.0)
                if stale_days > STALE_DAYS:
                    time_dev = min(1.0, (stale_days - STALE_DAYS) / STALE_DAYS)
            except (ValueError, TypeError):
                pass
        else:
            # 从未记录进展且目标设置已久 → 中性警告
            try:
                updated = datetime.fromisoformat(self._state.updated_at)
                if updated.tzinfo is None:
                    updated = updated.replace(tzinfo=timezone.utc)
                age_days = max((now - updated).total_seconds() / 86400.0, 0.0)
                if age_days > STALE_DAYS:
                    time_dev = min(0.5, (age_days - STALE_DAYS) / (STALE_DAYS * 2))
            except (ValueError, TypeError):
                pass

        # 加权合成（硬规则）：语义 0.5 + 行为 0.3 + 时间 0.2
        score = round(min(1.0, max(0.0,
            0.5 * semantic_dev + 0.3 * behavior_dev + 0.2 * time_dev)), 4)

        level = self._level_of(score)
        result = {
            "goal_deviation_score": score,
            "level": level,
            "goal": goal,
            "goal_source": self._state.source,
            "factors": {
                "semantic_deviation": round(semantic_dev, 4),
                "behavior_deviation": round(behavior_dev, 4),
                "time_decay_deviation": round(time_dev, 4),
            },
        }
        logger.info("目标偏离计算: score=%.3f level=%s", score, level)
        return result

    @staticmethod
    def _level_of(score: float) -> str:
        if score < 0.2:
            return LEVEL_GREEN
        if score < 0.4:
            return LEVEL_GRAY
        if score < 0.7:
            return LEVEL_YELLOW
        return LEVEL_RED


def resolve_goal(session_goal: Optional[str] = None,
                 g008_goals: Optional[List[str]] = None) -> Tuple[str, str]:
    """
    按优先级解析当前目标：
      session 会话级 > G008 灵元计划核心目标 > 默认目标
    返回 (goal_text, source)。
    """
    if session_goal and session_goal.strip():
        return session_goal.strip(), GOAL_SOURCE_SESSION
    if g008_goals:
        return g008_goals[0], GOAL_SOURCE_G008
    return DEFAULT_GOAL, GOAL_SOURCE_DEFAULT
