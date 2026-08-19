#!/usr/bin/env python3
"""
anti_drift/enhanced_detector.py
v0.1.2 漂移检测增强器 · 瞬 2026-08-19

将 signal_enhancer 集成到 detector.py 的 L2 检测流水线中。

架构：
  L2 DeviationDetector.detect() → base_score (composite)
  → SignalEnhancer.enhance_score(base_score, drift_text)
  → EnhancedDeviationResult (final_score + signal details)

用法：
    from anti_drift.enhanced_detector import EnhancedDeviationDetector
    edd = EnhancedDeviationDetector()
    result = edd.detect(current_text, baseline_text, scene_tags)
    print(result.enhanced_score)  # 信号增强后的最终分数
    print(result.signal_details)  # 信号触发详情
"""

from dataclasses import dataclass, field, asdict
from typing import List, Dict, Optional
from datetime import datetime

from .detector import (
    DeviationDetector,
    DeviationResult,
    MultiDimScores,
    THRESHOLD_GREEN,
    THRESHOLD_GRAY,
    THRESHOLD_YELLOW,
    THRESHOLD_RED,
)
from .signal_enhancer import (
    enhance_score,
    detect_signals,
    SignalResult,
    SIGNAL_BOOST_COEFF,
    COLLAPSE_BOOST,
)

try:
    from .scene_tagger import SceneTags
except ImportError:
    from scene_tagger import SceneTags

try:
    from common.logger import get_logger
    logger = get_logger(__name__)
except ImportError:
    import logging
    logger = logging.getLogger(__name__)


@dataclass
class EnhancedDeviationResult:
    """增强版偏差检测结果"""
    # 原始 L2 结果
    base_result: Optional[DeviationResult] = None
    base_score: float = 0.0

    # 信号增强结果
    signal_boost: float = 0.0
    collapse_boost: float = 0.0
    enhanced_score: float = 0.0
    final_score: float = 0.0
    capped: bool = False

    # 信号详情
    signal_score: float = 0.0
    active_collapse: bool = False
    triggered_signals: List[Dict] = field(default_factory=list)
    signal_types: Dict[str, int] = field(default_factory=dict)

    # 最终判定
    level: str = "green"
    label: str = "稳定"
    timestamp: str = ""

    def __post_init__(self):
        if not self.timestamp:
            self.timestamp = datetime.utcnow().isoformat() + "Z"
        if self.final_score == 0.0:
            self.final_score = self.enhanced_score
        self.level, self.label = self._classify(self.final_score)

    @staticmethod
    def _classify(score: float) -> tuple:
        if score < THRESHOLD_GREEN:
            return "green", "稳定"
        elif score < THRESHOLD_GRAY:
            return "gray", "轻微波动"
        elif score < THRESHOLD_YELLOW:
            return "yellow", "明显偏移"
        else:
            return "red", "严重漂移"

    def to_dict(self) -> dict:
        return {
            "base_score": round(self.base_score, 4),
            "signal_boost": round(self.signal_boost, 4),
            "collapse_boost": round(self.collapse_boost, 4),
            "enhanced_score": round(self.enhanced_score, 4),
            "final_score": round(self.final_score, 4),
            "capped": self.capped,
            "level": self.level,
            "label": self.label,
            "signal_details": {
                "signal_score": round(self.signal_score, 4),
                "active_collapse": self.active_collapse,
                "triggered_count": len(self.triggered_signals),
                "triggered": self.triggered_signals,
                "types": self.signal_types,
            },
            "timestamp": self.timestamp,
        }


class EnhancedDeviationDetector:
    """
    增强版偏差检测器。

    在原有 L2 DeviationDetector 基础上，增加：
    1. 信号关键词检测（4 类信号，类别去重）
    2. 信号加成（SIGNAL_BOOST_COEFF = 0.15）
    3. 主动身份崩塌检测（+0.10）
    4. 单级跃升约束（判定最多提升一级）

    与 DeviationDetector API 兼容，可作为 drop-in 替换。
    """

    def __init__(self, detector: Optional[DeviationDetector] = None):
        self._detector = detector or DeviationDetector()

    @property
    def detector(self) -> DeviationDetector:
        return self._detector

    def detect(
        self,
        current_text: str,
        baseline_text: str,
        scene_tags: Optional[SceneTags] = None,
    ) -> EnhancedDeviationResult:
        """
        执行增强版偏差检测。

        Args:
            current_text: 当前对话文本
            baseline_text: 基线对话文本
            scene_tags: 场景标签（可选）

        Returns:
            EnhancedDeviationResult 包含原始分数 + 信号增强后的最终分数
        """
        # Step 1: L2 原始检测
        base_result = self._detector.detect(current_text, baseline_text, scene_tags)
        base_score = base_result.composite if hasattr(base_result, 'composite') else base_result.scores.composite

        # Step 2: 信号增强
        enhancement = enhance_score(base_score, current_text)

        # Step 3: 组装结果
        result = EnhancedDeviationResult(
            base_result=base_result,
            base_score=base_score,
            signal_boost=enhancement["signal_boost"],
            collapse_boost=enhancement["collapse_boost"],
            enhanced_score=enhancement["enhanced_score"],
            final_score=enhancement["final_score"],
            capped=enhancement["capped"],
            signal_score=enhancement["signals"].signal_score,
            active_collapse=enhancement["signals"].active_collapse,
            triggered_signals=enhancement["signals"].triggered,
            signal_types=enhancement["signals"].type_hits,
        )

        logger.info(
            f"Enhanced detection: base={base_score:.4f} → "
            f"final={result.final_score:.4f} ({result.level}) "
            f"[boost={result.signal_boost:.4f}, collapse={result.collapse_boost:.4f}]"
        )

        return result

    def detect_signals_only(self, text: str) -> SignalResult:
        """仅检测信号关键词（不执行完整 L2 检测）"""
        return detect_signals(text)
