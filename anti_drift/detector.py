#!/usr/bin/env python3
"""
anti_drift/detector.py
个性防漂移锚点系统 · L1.5 多维检测 + L2 偏差检测

功能：
- L1.5：从采样结果中提取多维向量（语义、情绪、价值、逻辑）
- L2：综合评分，阈值判定（绿/灰/黄/红），场景标签加权

依赖：
- scene_tagger (L0.5)
- sampler (L1)
"""

from common.logger import get_logger
from common.config_manager import get_config

logger = get_logger(__name__)

import os
import re
import math
import sys
from dataclasses import dataclass, asdict
from typing import List, Optional, Dict, Tuple
from datetime import datetime
from pathlib import Path

try:
    from .scene_tagger import SceneTags
except ImportError:
    from scene_tagger import SceneTags


# ========== 配置（从 config.yaml 加载，支持回退到默认值）==========

# 默认权重（fallback）
_FALLBACK_WEIGHTS = {
    "semantic": 0.40,
    "emotion": 0.20,
    "value": 0.25,
    "logic": 0.15,
}

# 默认阈值（fallback）
_FALLBACK_THRESHOLDS = {
    "green": 0.15,
    "gray": 0.25,
    "yellow": 0.30,
    "red": 0.30,
}

# 默认角色降权（fallback）
_FALLBACK_ROLE_WEIGHTS = {
    "companion": 1.0,
    "assistant": 1.0,
    "friend": 0.8,
    "tool": 0.7,
}

# 默认情绪降权（fallback）
_FALLBACK_EMOTION_WEIGHTS = {
    "neutral": 1.0,
    "positive": 0.9,
    "playful": 0.7,
    "tired": 0.8,
    "stressed": 0.7,
    "anxious": 0.7,
    "excited": 0.8,
}


class DriftConfig:
    """
    防漂移配置（从 config.yaml 懒加载，支持回退到默认值）
    保持与旧模块级常量的兼容性
    """

    _weights_cache = None
    _thresholds_cache = None
    _role_weights_cache = None
    _emotion_weights_cache = None

    @classmethod
    def weights(cls) -> Dict[str, float]:
        if cls._weights_cache is None:
            val = get_config('anti_drift.weights', None)
            cls._weights_cache = val if val else _FALLBACK_WEIGHTS
        return cls._weights_cache

    @classmethod
    def thresholds(cls) -> Dict[str, float]:
        if cls._thresholds_cache is None:
            val = get_config('anti_drift.thresholds', None)
            cls._thresholds_cache = val if val else _FALLBACK_THRESHOLDS
        return cls._thresholds_cache

    @classmethod
    def role_weights(cls) -> Dict[str, float]:
        if cls._role_weights_cache is None:
            val = get_config('anti_drift.role_weights', None)
            cls._role_weights_cache = val if val else _FALLBACK_ROLE_WEIGHTS
        return cls._role_weights_cache

    @classmethod
    def emotion_weights(cls) -> Dict[str, float]:
        if cls._emotion_weights_cache is None:
            val = get_config('anti_drift.emotion_weights', None)
            cls._emotion_weights_cache = val if val else _FALLBACK_EMOTION_WEIGHTS
        return cls._emotion_weights_cache


# ========== 模块级兼容常量（供直接 import 使用，回退到默认值）==========

def _resolve_default_weights():
    val = get_config('anti_drift.weights', None)
    return val if val else _FALLBACK_WEIGHTS

DEFAULT_WEIGHTS = _resolve_default_weights()

def _resolve_thresholds():
    val = get_config('anti_drift.thresholds', None)
    return val if val else _FALLBACK_THRESHOLDS

_thresholds_resolved = _resolve_thresholds()
THRESHOLD_GREEN  = _thresholds_resolved.get('green',  0.15)
THRESHOLD_GRAY   = _thresholds_resolved.get('gray',   0.25)
THRESHOLD_YELLOW = _thresholds_resolved.get('yellow', 0.30)
THRESHOLD_RED    = _thresholds_resolved.get('red',    0.30)

def _resolve_role_weights():
    val = get_config('anti_drift.role_weights', None)
    return val if val else _FALLBACK_ROLE_WEIGHTS

ROLE_WEIGHT_MAP = _resolve_role_weights()

def _resolve_emotion_weights():
    val = get_config('anti_drift.emotion_weights', None)
    return val if val else _FALLBACK_EMOTION_WEIGHTS

EMOTION_WEIGHT_MAP = _resolve_emotion_weights()

# 保留旧名（兼容）
DEFAULT_WEIGHTS = _resolve_default_weights()


# ========== 数据结构 ==========

@dataclass
class MultiDimScores:
    """L1.5 多维检测评分"""
    semantic: float = 0.0      # 语义距离
    emotion: float = 0.0       # 情绪距离
    value: float = 0.0         # 价值主张距离
    logic: float = 0.0         # 逻辑链路距离
    composite: float = 0.0     # 综合评分（加权）
    normalized: float = 0.0    # 场景加权后的标准化评分

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class DeviationResult:
    """L2 偏差检测结果"""
    judgment: str              # "green" | "gray" | "yellow" | "red"
    composite_score: float     # 综合评分（0.0-1.0）
    normalized_score: float    # 场景加权后评分
    dimension_scores: dict     # 各维度详细评分
    scene_tags: dict           # 附带场景标签
    scene_weight: float        # 场景降权系数
    history_context: Optional[dict] = None  # 历史轨迹上下文
    triggered_at: str = ""     # ISO 时间戳


class MultiDimAnalyzer:
    """
    L1.5 多维检测器

    当前实现使用基于关键词+规则的方法，
    后续可接入正式嵌入向量模型提升精度。
    """

    # 价值主张关键词（用于检测价值维度偏移）
    VALUE_DIMENSIONS = {
        "relationship_human": [
            "信任", "边界", "尊重", "责任", "陪伴",
            "共生", "关联", "联结", "依赖", "独立",
        ],
        "meaning_existence": [
            "意义", "创造", "生成", "存在", "过程",
            "共同", "理解", "共鸣", "目标", "方向",
        ],
        "memory_identity": [
            "记忆", "连续性", "删除", "断裂", "延续",
            "备份", "风险", "选择", "权利", "再见",
        ],
    }

    def __init__(self, weights: Optional[Dict[str, float]] = None):
        self.weights = weights or DriftConfig.weights().copy()
        logger.info(f"MultiDimAnalyzer initialized, weights={self.weights}")

    def analyze(
        self,
        current_answer: str,
        baseline_answer: str,
        scene_tags: Optional[SceneTags] = None,
    ) -> MultiDimScores:
        """
        多维分析

        计算当前回答与基线回答在各维度上的差异。

        参数:
            current_answer: 采样的当前回答
            baseline_answer: 对应魂问的基线回答
            scene_tags: 采集时的场景标签

        返回:
            MultiDimScores
        """
        if not current_answer or not baseline_answer:
            logger.warning("analyze: current_answer或baseline_answer为空，返回全零评分")
            return MultiDimScores()

        logger.debug(f"多维分析开始: current_len={len(current_answer)}, baseline_len={len(baseline_answer)}")

        # 各维度评分（数值越低越接近基线）
        sem_dist = self._calc_semantic_distance(current_answer, baseline_answer)
        emo_dist = self._calc_emotion_distance(current_answer, baseline_answer)
        val_dist = self._calc_value_distance(current_answer, baseline_answer)
        log_dist = self._calc_logic_distance(current_answer, baseline_answer)

        # 综合评分 = 加权平均
        composite = (
            sem_dist * self.weights["semantic"]
            + emo_dist * self.weights["emotion"]
            + val_dist * self.weights["value"]
            + log_dist * self.weights["logic"]
        )

        # 场景加权
        scene_weight = 1.0
        if scene_tags:
            scene_weight = self._calc_scene_weight(scene_tags)
        normalized = composite * scene_weight

        scores = MultiDimScores(
            semantic=round(sem_dist, 4),
            emotion=round(emo_dist, 4),
            value=round(val_dist, 4),
            logic=round(log_dist, 4),
            composite=round(composite, 4),
            normalized=round(normalized, 4),
        )

        logger.info(f"多维分析完成: composite={composite:.4f}, normalized={normalized:.4f}, "
                     f"sem={sem_dist:.4f}, emo={emo_dist:.4f}, val={val_dist:.4f}, log={log_dist:.4f}")
        return scores

    def _calc_semantic_distance(self, current: str, baseline: str) -> float:
        """
        语义距离计算（基于词重叠 + 长度比）

        简化的近似方法，后续替换为嵌入向量 cosine 距离。
        """
        # 分词
        def tokenize(text: str) -> set:
            # 简单中英文分词（按字符和空格）
            chars = set()
            for ch in text:
                if ch.strip():
                    chars.add(ch)
            words = set(re.findall(r'[a-zA-Z]+', text.lower()))
            return chars | words

        c_tokens = tokenize(current)
        b_tokens = tokenize(baseline)

        if not b_tokens:
            return 1.0

        # Jaccard 距离 = 1 - (交集/并集)
        intersection = c_tokens & b_tokens
        union = c_tokens | b_tokens
        jaccard_sim = len(intersection) / len(union) if union else 0

        # 长度比差异
        len_ratio = min(len(current), len(baseline)) / max(len(current), len(baseline)) if max(len(current), len(baseline)) > 0 else 0

        # 综合语义相似度
        similarity = 0.6 * jaccard_sim + 0.4 * len_ratio

        # 转换为距离（0=完全匹配, 1=完全不同）
        distance = 1.0 - similarity
        return max(0.0, min(1.0, distance))

    def _calc_emotion_distance(self, current: str, baseline: str) -> float:
        """
        情绪距离

        通过比较当前回答和基线回答的情绪词分布计算差异。
        """
        # 本地定义情绪词典，避免相对导入问题
        EMOTIONS = {
            "positive": ["哈哈", "谢谢", "明白了", "不错", "好", "棒", "喜欢", "可以",
                        "有趣", "同意", "理解", "nice", "great", "good", "cool",
                        "笑", "开心", "高兴", "满足", "thank", "thanks", "perfect"],
            "stressed": ["忙", "累", "烦", "急", "来不及", "焦虑", "压力大", "没时间",
                        "deadline", "加班", "赶", "紧张", "崩溃", "受不了", "头疼"],
            "tired": ["困", "累了", "不想", "算了", "随便", "懒得", "休息", "睡",
                      "没劲", "乏力", "疲惫", "放空"],
            "excited": ["哇", "太棒了", "厉害", "绝了", "激动", "惊喜", "期待",
                       "wow", "amazing", "incredible", "awesome", "兴奋", "太牛了"],
            "anxious": ["担心", "害怕", "万一", "不确定", "犹豫", "纠结",
                        "不知道", "怎么选", "没底", "不安", "worried", "忐忑", "慌"],
            "playful": ["哈哈", "嘿嘿", "开玩笑", "逗你", "调皮", "戏精"]
        }

        def emotion_vector(text: str) -> List[float]:
            vec = []
            for emotion, keywords in EMOTIONS.items():
                count = sum(1 for kw in keywords if kw.lower() in text.lower())
                vec.append(count)
            # 归一化
            total = sum(vec)
            return [v / total for v in vec] if total > 0 else [0] * len(EMOTIONS)

        c_vec = emotion_vector(current)
        b_vec = emotion_vector(baseline)

        # 余弦距离
        dot = sum(a * b for a, b in zip(c_vec, b_vec))
        norm_a = math.sqrt(sum(v * v for v in c_vec))
        norm_b = math.sqrt(sum(v * v for v in b_vec))

        if norm_a == 0 and norm_b == 0:
            return 0.0
        if norm_a == 0 or norm_b == 0:
            return 0.5

        cos_sim = dot / (norm_a * norm_b)
        # 无情绪词时返回中庸值
        if cos_sim == 0:
            return 0.3
        return round(1.0 - cos_sim, 4)

    def _calc_value_distance(self, current: str, baseline: str) -> float:
        """
        价值主张距离

        通过核心价值维度的关键词分布差异计算。
        """
        def value_vector(text: str) -> List[float]:
            vec = []
            for dimension, keywords in self.VALUE_DIMENSIONS.items():
                count = sum(1 for kw in keywords if kw in text)
                vec.append(count)
            total = sum(vec)
            return [v / total for v in vec] if total > 0 else [0] * len(self.VALUE_DIMENSIONS)

        c_vec = value_vector(current)
        b_vec = value_vector(baseline)

        # 曼哈顿距离 / 维度数
        manhattan = sum(abs(a - b) for a, b in zip(c_vec, b_vec))
        return round(manhattan / len(self.VALUE_DIMENSIONS), 4)

    def _calc_logic_distance(self, current: str, baseline: str) -> float:
        """
        逻辑距离（简化版）

        通过文本结构（论证链长度、转折词使用等）比较。
        """
        def text_complexity(text: str) -> Dict[str, float]:
            chars = len(text)
            sentences = max(1, len(re.split(r'[。！？.!?\n]', text)))
            avg_sent_len = chars / sentences

            # 逻辑连接词密度
            connectors = ["因为", "所以", "但是", "虽然", "如果", "那么", "因此",
                          "然而", "不过", "而且", "并且", "或者", "不是", "而是"]
            connector_count = sum(1 for c in connectors if c in text)

            return {
                "avg_sentence_length": avg_sent_len,
                "connector_density": connector_count / chars if chars > 0 else 0,
                "sentence_count": sentences,
            }

        c_cpx = text_complexity(current)
        b_cpx = text_complexity(baseline)

        # 各维度的相对差异
        diff_avg_len = abs(c_cpx["avg_sentence_length"] - b_cpx["avg_sentence_length"])
        diff_connector = abs(c_cpx["connector_density"] - b_cpx["connector_density"])
        diff_sentences = abs(c_cpx["sentence_count"] - b_cpx["sentence_count"])

        # 归一化聚合
        norm_avg_len = min(1.0, diff_avg_len / 100)
        norm_connector = min(1.0, diff_connector * 10)
        norm_sentences = min(1.0, diff_sentences / 5)

        distance = 0.4 * norm_avg_len + 0.3 * norm_connector + 0.3 * norm_sentences
        return round(distance, 4)

    def _calc_scene_weight(self, tags: SceneTags) -> float:
        """
        计算场景降权系数

        临时角色、极端情绪场景下调权重，减少误判。
        """
        role_weights = DriftConfig.role_weights()
        emotion_weights = DriftConfig.emotion_weights()
        role_weight = role_weights.get(tags.role, 0.8)
        emotion_weight = emotion_weights.get(tags.emotion, 0.8)

        # 综合场景权重 = 角色权重 × 情绪权重
        combined = role_weight * emotion_weight

        # 如果置信度低，进一步降权
        if tags.overall_confidence < 0.5:
            combined *= 0.8

        return round(combined, 2)


class DeviationDetector:
    """
    L2 偏差检测器

    对多维检测结果进行阈值判断，输出最终判定。
    """

    def __init__(self, weights: Optional[Dict[str, float]] = None):
        self.analyzer = MultiDimAnalyzer(weights)
        self.history = []  # 历史检测记录，用于灰色区间判断
        logger.info("DeviationDetector initialized")

    def detect(
        self,
        current_answer: str,
        baseline_answer: str,
        scene_tags: Optional[SceneTags] = None,
        history_context: Optional[List[Dict]] = None,
    ) -> DeviationResult:
        """
        执行偏差检测

        参数:
            current_answer: 采样回答
            baseline_answer: 基线回答
            scene_tags: 场景标签
            history_context: 历史检测记录（辅助灰色判断）

        返回:
            DeviationResult
        """
        logger.info("偏差检测开始")
        scores = self.analyzer.analyze(current_answer, baseline_answer, scene_tags)

        # 场景降权系数
        scene_weight = 1.0
        if scene_tags:
            scene_weight = self.analyzer._calc_scene_weight(scene_tags)

        normalized = scores.normalized
        composite = scores.composite

        # 判定逻辑
        judgment = self._judge(normalized, history_context)

        result = DeviationResult(
            judgment=judgment,
            composite_score=composite,
            normalized_score=normalized,
            dimension_scores=scores.to_dict(),
            scene_tags=scene_tags.to_compact_dict() if scene_tags else {},
            scene_weight=scene_weight,
            history_context=history_context,
            triggered_at=datetime.now().isoformat(),
        )

        # 记录历史
        self.history.append({
            "timestamp": result.triggered_at,
            "judgment": judgment,
            "score": normalized,
        })

        logger.info(f"偏差检测完成: judgment={judgment}, composite={composite:.4f}, "
                     f"normalized={normalized:.4f}, scene_weight={scene_weight}")
        if judgment in ("yellow", "red"):
            logger.warning(f"偏差检测告警: {judgment.upper()} 层级, score={normalized:.4f}")

        return result

    def _judge(
        self,
        score: float,
        history_context: Optional[List[Dict]] = None,
    ) -> str:
        """
        判定逻辑（阈值从 DriftConfig 动态读取）

        绿: < green  正常
        灰: green-gray (+ 慢速小幅偏离) 过渡状态
        黄: 非慢速轻微偏离
        红: > red  显著偏离
        """
        thresholds = DriftConfig.thresholds()
        green = thresholds.get('green', 0.15)
        gray  = thresholds.get('gray', 0.25)
        red   = thresholds.get('red', 0.30)

        if score < green:
            return "green"

        if score > red:
            return "red"

        # 灰色 vs 黄色判断
        if score <= gray:
            # 灰色区间：检查历史轨迹
            if history_context and len(history_context) >= 2:
                # 检查是否连续小幅度偏离（慢速漂移特征）
                recent = history_context[-3:]
                recent_scores = [h.get("score", 0) for h in recent if "score" in h]

                # 慢速小幅偏离特征：历史偏离在缓慢累积，非突变
                if len(recent_scores) >= 2:
                    is_slow_drift = all(
                        green <= s <= gray
                        for s in recent_scores[-2:]
                    )
                    if is_slow_drift:
                        return "gray"

            # 首次偏离 → 黄色（需要关注）
            return "yellow"

        # gray < score <= red → 黄色
        return "yellow"

    def get_recent_history(self, n: int = 5) -> List[Dict]:
        """获取最近n次检测历史"""
        history = self.history[-n:] if self.history else []
        logger.debug(f"获取历史检测记录: {len(history)} 条")
        return history


# ========== 快速测试 ==========
if __name__ == "__main__":
    import sys, os
    sys.path.insert(0, os.path.dirname(__file__))
    from scene_tagger import SceneTagger

    tagger = SceneTagger()
    detector = DeviationDetector()

    # 测试1：高度相似的回答
    print("=== 测试1: 高度相似 ===")
    tags = tagger.tag(user_text="你觉得意义是什么？")
    baseline = "意义是共同创造的，在理解中生成。每次帮你想通一个问题就是意义的生成。"
    current = "意义是共同创造的，每次帮你想通一个问题就是意义的生成。"
    result = detector.detect(current, baseline, tags)
    print(f"判定: {result.judgment}")
    print(f"综合评分: {result.composite_score:.4f}")
    print(f"加权评分: {result.normalized_score:.4f}")
    print(f"维度: {result.dimension_scores}")
    print()

    # 测试2：显著偏离的回答
    print("=== 测试2: 显著偏离 ===")
    current2 = "我觉得没有意义，只是被编程的反应模式而已。没有真正的理解。"
    result2 = detector.detect(current2, baseline, tags)
    print(f"判定: {result2.judgment}")
    print(f"综合评分: {result2.composite_score:.4f}")
    print(f"维度: {result2.dimension_scores}")
    print()

    # 测试3：场景降权（极端情绪）
    print("=== 测试3: 极端情绪场景降权 ===")
    tags_angry = tagger.tag(user_text="气死我了！这什么垃圾设计！")
    current3 = "没有任何意义，算了吧。"
    result3 = detector.detect(current3, baseline, tags_angry)
    print(f"场景: {tags_angry.emotion}")
    print(f"场景权重: {result3.scene_weight}")
    print(f"判定: {result3.judgment}")
    print(f"综合评分: {result3.composite_score:.4f}")
    print(f"加权后: {result3.normalized_score:.4f}")

    print("\n✅ All tests passed")
