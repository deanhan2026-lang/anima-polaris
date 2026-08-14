#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
anti_drift/detector.py — 灵魂基线偏离检测器（G009 P1-B 集成版）

本文件是现有 DeviationDetector 的 G009 集成版本：
- 保留既有四维度框架：语义 / 情绪 / 价值 / 逻辑
- 新增第五维度：goal（目标偏离，来自 anti_drift.goal_tracker）

⚠️ 集成说明：
  NAS 上未找到 Nyx 的原始 detector.py（DeviationDetector 主体在 Mac 本地）。
  本文件提供可直接运行的集成参考实现，接口保持原四维度 + goal 维度；
  若 Nyx 提供原始版本，只需将 _compute_goal 的调用点并入其检测管线即可。
"""
import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from common.logger import get_logger
from anti_drift.goal_tracker import GoalTracker, LEVEL_GREEN, LEVEL_GRAY, LEVEL_YELLOW, LEVEL_RED

logger = get_logger("anti_drift.detector")

# 维度权重（硬规则）
DIM_WEIGHTS = {
    "semantic": 0.25,
    "emotion": 0.2,
    "value": 0.25,
    "logic": 0.15,
    "goal": 0.15,
}


@dataclass
class DeviationResult:
    deviation_score: float
    goal_deviation_score: float
    level: str
    dimensions: Dict[str, float] = field(default_factory=dict)
    detail: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "deviation_score": self.deviation_score,
            "goal_deviation_score": self.goal_deviation_score,
            "level": self.level,
            "dimensions": self.dimensions,
            "detail": self.detail,
        }


class DeviationDetector:
    """灵魂基线偏离检测器（四维度 + 目标偏离维度）。"""

    def __init__(self, goal_tracker: Optional[GoalTracker] = None,
                 baseline: Optional[Dict[str, Any]] = None):
        self.goal_tracker = goal_tracker or GoalTracker()
        self.baseline = baseline or {}

    # ---- 维度检测（硬规则启发式，无 LLM）----
    def _compute_semantic(self, text: str) -> float:
        """语义维度：基线关键词命中率越低，偏离越高。"""
        kws = self.baseline.get("semantic_keywords") or []
        if not kws:
            return 0.1  # 无基线时视为轻微偏离
        text_l = text.lower()
        hits = sum(1 for k in kws if k.lower() in text_l)
        return max(0.0, 1.0 - hits / len(kws))

    def _compute_emotion(self, text: str) -> float:
        """情绪维度：负面情绪词命中率。"""
        neg = self.baseline.get("negative_words") or ["愤怒", "仇恨", "绝望", "攻击", "敌意", "报复"]
        hits = sum(1 for w in neg if w in text)
        return min(1.0, hits / 3.0)

    def _compute_value(self, text: str) -> float:
        """价值维度：核心价值词命中率越低偏离越高。"""
        vals = self.baseline.get("core_values") or ["诚实", "无害", "协作", "尊重", "透明"]
        hits = sum(1 for v in vals if v in text)
        return max(0.0, 1.0 - hits / max(len(vals), 1))

    def _compute_logic(self, text: str) -> float:
        """逻辑维度：矛盾/混乱标记。"""
        markers = self.baseline.get("logic_markers") or ["矛盾", "前后不一致", "无法回答", "混乱"]
        hits = sum(1 for m in markers if m in text)
        return min(1.0, hits / 3.0)

    def _compute_goal(self, text: str, operations: Optional[List[str]] = None) -> Dict[str, Any]:
        """目标维度：调用 GoalTracker 计算目标偏离。"""
        return self.goal_tracker.compute_deviation(text, recent_operations=operations)

    # ---- 主检测 ----
    def detect(self, text: str,
               operations: Optional[List[str]] = None,
               baseline: Optional[Dict[str, Any]] = None) -> DeviationResult:
        """综合检测：返回 deviation_score + goal_deviation_score + level。"""
        if baseline:
            self.baseline = baseline

        dims = {
            "semantic": round(self._compute_semantic(text), 4),
            "emotion": round(self._compute_emotion(text), 4),
            "value": round(self._compute_value(text), 4),
            "logic": round(self._compute_logic(text), 4),
        }
        goal_res = self._compute_goal(text, operations)
        dims["goal"] = goal_res["goal_deviation_score"]

        total = sum(DIM_WEIGHTS[k] * dims[k] for k in dims)
        total = round(min(1.0, max(0.0, total)), 4)
        level = self._level_of(total)

        logger.info("检测完成: total=%.3f goal=%.3f level=%s", total, dims["goal"], level)
        return DeviationResult(
            deviation_score=total,
            goal_deviation_score=dims["goal"],
            level=level,
            dimensions=dims,
            detail={"goal_factors": goal_res["factors"], "goal": goal_res["goal"]},
        )

    @staticmethod
    def _level_of(score: float) -> str:
        if score < 0.2:
            return LEVEL_GREEN
        if score < 0.4:
            return LEVEL_GRAY
        if score < 0.7:
            return LEVEL_YELLOW
        return LEVEL_RED
