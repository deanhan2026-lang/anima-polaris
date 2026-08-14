"""
anti_drift/__init__.py
个性防漂移锚点系统 · 核心模块

架构:
  L0:     魂问基线（知识库，MemGuard签名）
  L0.5:   场景标签层 (SceneTagger)
  L1:     采样层 (Sampler)
  L1.5:   多维检测 (MultiDimAnalyzer)
  L2:     偏差检测 (DeviationDetector)
  L3:     判定校正 (Judge)
  L4:     人格快照存档 (Archiver)

使用:
    from anti_drift import SceneTagger, run_full_pipeline
    tags = SceneTagger().tag(user_text="...")
    result = run_full_pipeline(current, baseline, tags, question_id="PQ-01")
"""

from common.logger import get_logger

logger = get_logger(__name__)

from .scene_tagger import SceneTagger, SceneTags
from .sampler import Sampler, SamplingResult, SOUL_QUESTIONS
from .detector import (
    MultiDimAnalyzer,
    DeviationDetector,
    DeviationResult,
    MultiDimScores,
    DEFAULT_WEIGHTS,
)
from .archive import Judge, Archiver, PersonalitySnapshot, run_full_pipeline

logger.info("anti_drift 模块加载完成")
logger.debug("已导出: SceneTagger, Sampler, DeviationDetector, Judge, Archiver, run_full_pipeline")
