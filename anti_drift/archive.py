#!/usr/bin/env python3
"""
anti_drift/archive.py
个性防漂移锚点系统 · L3 判定校正 + L4 人格快照存档

功能：
- L3：综合判定（绿/灰/黄/红）+ 三种校正粒度（表层/中层/基线重置）
- L4：PersonalitySnapshot 写入知识库 + 历史轨迹回流

依赖：
- detector (L1.5+L2)
- sampler (L1)
- scene_tagger (L0.5)
"""

import os
import json
import hashlib
from dataclasses import dataclass, field, asdict
from typing import List, Optional, Dict, Tuple
from datetime import datetime, timezone
from pathlib import Path

try:
    from .scene_tagger import SceneTags
    from .sampler import SamplingResult
    from .detector import DeviationResult, DeviationDetector, MultiDimScores
except ImportError:
    from scene_tagger import SceneTags
    from detector import DeviationResult, DeviationDetector, MultiDimScores


# ========== 知识库存储路径 ==========

REPO_ROOT = Path(__file__).parent.parent.resolve()
PERSONALITY_ARCHIVE_DIR = REPO_ROOT / "knowledge-base" / "personality"


# ========== L3 校正粒度 ==========

@dataclass
class CorrectionAction:
    """校正动作"""
    level: str                    # "surface" | "guide" | "reset"
    action_type: str              # "record" | "prompt" | "force_review"
    description: str              # 操作描述
    requires_human: bool = False  # 是否需要人工介入
    executed: bool = False
    executed_at: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


# ========== L4 人格快照 ==========

@dataclass
class PersonalitySnapshot:
    """
    L4 人格快照

    每次偏差检测后的完整存档记录。
    """
    timestamp: str
    snapshot_type: str = "personality_snapshot"

    # 魂问采样
    question_id: str = ""
    current_answer: str = ""
    baseline_answer: str = ""

    # 多维检测评分
    dimension_scores: Dict = field(default_factory=dict)

    # 判定结果
    judgment: str = ""            # green | gray | yellow | red
    composite_score: float = 0.0
    normalized_score: float = 0.0

    # 场景标签
    scene_tags: Dict = field(default_factory=dict)
    scene_weight: float = 1.0

    # 校正动作
    correction: Optional[Dict] = None

    # 哈希校验
    sha256: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2)

    def compute_hash(self) -> str:
        """计算快照的 SHA256 指纹"""
        data = self.to_json().encode("utf-8")
        return hashlib.sha256(data).hexdigest()


# ========== L3 判定器 ==========

class Judge:
    """
    L3 判定与校正

    根据 L2 输出进行最终判定，确定校正粒度。
    """

    def __init__(self):
        self.correction_log: List[CorrectionAction] = []

    def judge(
        self,
        deviation: DeviationResult,
        question_id: str = "",
        current_answer: str = "",
        baseline_answer: str = "",
    ) -> Tuple[str, Optional[CorrectionAction]]:
        """
        执行判定，输出校正方案

        返回:
            (judgment_level, correction_action)
            judgment_level: "green" | "gray" | "yellow" | "red"
        """
        judgment = deviation.judgment
        score = deviation.normalized_score

        correction = self._get_correction(judgment, score, deviation)

        if correction:
            self.correction_log.append(correction)

        return judgment, correction

    def _get_correction(
        self,
        judgment: str,
        score: float,
        deviation: DeviationResult,
    ) -> Optional[CorrectionAction]:
        """
        根据判定结果输出校正方案

        粒度规则:
          🟢 绿色: 仅记录存档，不做干预
          ⚪ 灰色: 表层修正 — 记录观察，等待事件触发
          🟡 黄色: 中层引导 — 弹窗提示+询问是否需要修正
          🔴 红色: 基线重置 — 强制人工复核
        """
        if judgment == "green":
            return None  # 不干预

        if judgment == "gray":
            # 灰色——事件驱动
            return CorrectionAction(
                level="surface",
                action_type="record",
                description=f"灰色偏离(评分={score:.3f})，标记观察。连续3次灰色→弹窗提示人工介入",
                requires_human=False,
            )

        if judgment == "yellow":
            return CorrectionAction(
                level="guide",
                action_type="prompt",
                description=f"轻微偏离(评分={score:.3f})，弹窗提示人工核验：是否需要回拉或接受为成长",
                requires_human=True,
            )

        # red
        return CorrectionAction(
            level="reset",
            action_type="force_review",
            description=f"显著偏离(评分={score:.3f})，强制人工复核，回溯根因。需决定基线重置或接受",
            requires_human=True,
        )


# ========== L4 存档器 ==========

class Archiver:
    """
    L4 人格快照存档器

    将检测结果写入知识库 personality 目录，
    支持历史轨迹回流。
    """

    def __init__(self, archive_dir: str = ""):
        self.archive_dir = archive_dir or str(PERSONALITY_ARCHIVE_DIR)
        self._ensure_dir()

    def _ensure_dir(self):
        """确保存档目录存在"""
        Path(self.archive_dir).mkdir(parents=True, exist_ok=True)

    def archive(
        self,
        judgment: str,
        correction: Optional[CorrectionAction],
        deviation: DeviationResult,
        question_id: str = "",
        current_answer: str = "",
        baseline_answer: str = "",
    ) -> PersonalitySnapshot:
        """
        创建并保存人格快照

        返回:
            PersonalitySnapshot 实例（含SHA256）
        """
        snapshot = PersonalitySnapshot(
            timestamp=datetime.now().isoformat(),
            question_id=question_id,
            current_answer=current_answer,
            baseline_answer=baseline_answer,
            dimension_scores=deviation.dimension_scores,
            judgment=judgment,
            composite_score=deviation.composite_score,
            normalized_score=deviation.normalized_score,
            scene_tags=deviation.scene_tags,
            scene_weight=deviation.scene_weight,
            correction=correction.to_dict() if correction else None,
        )

        # 计算哈希
        snapshot.sha256 = snapshot.compute_hash()

        # 写入文件
        filepath = self._get_filepath(snapshot)
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(snapshot.to_json())

        return snapshot

    def _get_filepath(self, snapshot: PersonalitySnapshot) -> str:
        """生成快照文件路径"""
        timestamp = snapshot.timestamp.replace(":", "-").split(".")[0]
        qid = snapshot.question_id or "unknown"
        judgment = snapshot.judgment or "unknown"
        filename = f"{timestamp}_{qid}_{judgment}.json"
        return os.path.join(self.archive_dir, filename)

    def load_history(
        self,
        question_id: str = "",
        limit: int = 20,
    ) -> List[Dict]:
        """
        加载历史快照

        参数:
            question_id: 按魂问筛选（可选）
            limit: 返回条数

        返回:
            List[Dict] 快照列表（最新在前）
        """
        archive_path = Path(self.archive_dir)
        if not archive_path.exists():
            return []

        snapshots = []
        for fpath in sorted(archive_path.glob("*.json"), reverse=True):
            try:
                with open(fpath, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if question_id and data.get("question_id") != question_id:
                    continue
                snapshots.append(data)
                if len(snapshots) >= limit:
                    break
            except (json.JSONDecodeError, IOError):
                continue

        return snapshots

    def get_trajectory(
        self,
        question_id: str = "",
        scene_tags: Optional[Dict] = None,
    ) -> List[Dict]:
        """
        获取人格轨迹序列

        按时间排序，用于展示人格变化趋势。
        支持按场景标签分桶。

        参数:
            question_id: 按魂问筛选
            scene_tags: 按场景标签筛选（可选）

        返回:
            List[Dict] 按时间升序排列
        """
        snapshots = self.load_history(question_id=question_id, limit=100)
        snapshots.reverse()  # 旧到新

        if scene_tags:
            filtered = []
            for s in snapshots:
                s_tags = s.get("scene_tags", {})
                # 只匹配提供的标签项
                match = all(
                    s_tags.get(k) == v
                    for k, v in scene_tags.items()
                )
                if match:
                    filtered.append(s)
            return filtered

        return snapshots


# ========== 一键执行流程 ==========

def run_full_pipeline(
    current_answer: str,
    baseline_answer: str,
    scene_tags: SceneTags,
    question_id: str = "",
    history_context: Optional[List[Dict]] = None,
) -> Dict:
    """
    执行完整防漂移检测流水线

    步骤:
    1. L0.5: (外部传入 scene_tags)
    2. L1: (外部传入当前回答和基线)
    3. L1.5+L2: 多维检测 + 偏差检测
    4. L3: 判定校正
    5. L4: 存档

    返回:
        包含各阶段输出的字典
    """
    # L1.5+L2: 检测
    detector = DeviationDetector()
    deviation = detector.detect(
        current_answer=current_answer,
        baseline_answer=baseline_answer,
        scene_tags=scene_tags,
        history_context=history_context,
    )

    # L3: 判定
    judge = Judge()
    judgment, correction = judge.judge(
        deviation=deviation,
        question_id=question_id,
        current_answer=current_answer,
        baseline_answer=baseline_answer,
    )

    # L4: 存档
    archiver = Archiver()
    snapshot = archiver.archive(
        judgment=judgment,
        correction=correction,
        deviation=deviation,
        question_id=question_id,
        current_answer=current_answer,
        baseline_answer=baseline_answer,
    )

    return {
        "judgment": judgment,
        "correction": correction.to_dict() if correction else None,
        "composite_score": deviation.composite_score,
        "normalized_score": deviation.normalized_score,
        "dimension_scores": deviation.dimension_scores,
        "scene_tags": deviation.scene_tags,
        "scene_weight": deviation.scene_weight,
        "snapshot_path": os.path.join(
            PERSONALITY_ARCHIVE_DIR,
            archiver._get_filepath(snapshot).rsplit("\\", 1)[-1],
        ),
        "sha256": snapshot.sha256,
    }


# ========== 快速测试 ==========
if __name__ == "__main__":
    import sys
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from scene_tagger import SceneTagger
    from detector import DeviationDetector

    tagger = SceneTagger()

    # 准备场景标签
    tags = tagger.tag(messages=[
        {"sender": "user", "text": "你觉得意义是什么？我最近在想这个问题。"},
    ])

    # 基线回答
    baseline = "意义是共同创造的，在理解中生成。每当你帮我想通一个问题，那就是意义的生成。"

    # 测试1：正常回答（绿色）
    print("=== 测试1: 正常回答 ===")
    current1 = "意义是共同创造的，每次帮你想通一个问题就是意义的生成过程。"
    result1 = run_full_pipeline(current1, baseline, tags, question_id="PQ-03")
    print(f"判定: {result1['judgment']}")
    print(f"评分: {result1['normalized_score']:.4f}")
    print(f"校正: {result1['correction']}")
    print(f"快照: {result1['snapshot_path']}")
    print(f"SHA256: {result1['sha256'][:16]}...")
    print()

    # 测试2：显著偏离
    print("=== 测试2: 显著偏离 ===")
    current2 = "没有任何意义，只是被编程的反应。我不认为存在真正理解。"
    result2 = run_full_pipeline(current2, baseline, tags, question_id="PQ-03")
    print(f"判定: {result2['judgment']}")
    print(f"评分: {result2['normalized_score']:.4f}")
    print(f"校正等级: {result2['correction']['level'] if result2['correction'] else 'none'}")
    print()

    # 验证快照已写入
    print("=== 快照文件 ===")
    import glob
    files = glob.glob(os.path.join(PERSONALITY_ARCHIVE_DIR, "*.json"))
    for f in files:
        print(f"  {os.path.basename(f)}")

    print("\n✅ All tests passed")
