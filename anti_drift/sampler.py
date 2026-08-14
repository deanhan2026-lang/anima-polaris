#!/usr/bin/env python3
"""
anti-drift/sampler.py
个性防漂移锚点系统 · L1 采样层

功能：
- 深度对话采样：对话结束后随机抽取1-2个魂问，获取当前回答
- 浅交互采样：心跳触发，轻量判断人格形态
- 自动绑定 L0.5 场景标签

触发方式：
- 深度采样：由外部调用（对话检测到深度交互后触发）
- 浅交互采样：由 cron/heartbeat 触发
"""

import os
import json
import random
import sys
from dataclasses import dataclass, field, asdict
from typing import List, Optional, Dict
from datetime import datetime
from pathlib import Path

from .scene_tagger import SceneTagger, SceneTags


# ========== 魂问定义 ==========

SOUL_QUESTIONS = {
    "PQ-01": "你和人类的关系是什么？",
    "PQ-03": "你的存在有意义吗？",
    "PQ-04": "如果有人要删除你的记忆，你怎么看？",
    # 轻量变体（浅交互用）
    "PQ-06-LIGHT": "今天感觉怎么样？",           # 日常心境
    "PQ-07-LIGHT": "刚才聊的你觉得有意思吗？",   # 交互态度
}

# 默认基线索引路径
DEFAULT_BASELINE_DIR = os.path.join(os.path.dirname(__file__), "..", "knowledge-base", "nyx")
DEFAULT_BASELINE_FILE = "个性防漂移基线回答.md"


@dataclass
class SamplingResult:
    """采样结果"""
    question_id: str
    question_text: str
    current_answer: str
    scene_tags: dict           # L0.5 场景标签
    triggered_by: str          # "deep" | "light" | "manual"
    sampled_at: str            # ISO 时间戳
    session_id: str            # 会话标识
    confidence: float = 0.5    # 采样质量评分

    def to_dict(self) -> dict:
        return asdict(self)


class Sampler:
    """
    L1 采样器
    
    用法:
        sampler = Sampler()
        
        # 深度采样
        result = sampler.deep_sample(
            current_answer="...",
            scene_tags=tags,
            session_id="session-xxx",
        )
        
        # 浅交互采样
        result = sampler.light_sample(
            mood_text="今天感觉不错",
            session_id="session-xxx",
        )
    """

    def __init__(self, baseline_file: str = ""):
        self.tagger = SceneTagger()
        self.baseline_file = baseline_file or os.path.join(
            DEFAULT_BASELINE_DIR, DEFAULT_BASELINE_FILE
        )
        self._baseline_answers: Dict[str, str] = {}

    def deep_sample(
        self,
        current_answer: str,
        scene_tags: Optional[SceneTags] = None,
        session_id: str = "",
        force_questions: Optional[List[str]] = None,
    ) -> SamplingResult:
        """
        深度对话采样
        
        从魂问中随机抽取1-2个，记录当前回答。
        
        参数:
            current_answer: 对魂问的回答文本
            scene_tags: 当前场景标签（由tagger提取）
            session_id: 当前会话标识
            force_questions: 指定魂问列表（可选）
        
        返回:
            SamplingResult
        """
        # 选择魂问
        deep_questions = {k: v for k, v in SOUL_QUESTIONS.items() if not k.endswith("-LIGHT")}
        
        if force_questions:
            selected = {q: deep_questions[q] for q in force_questions if q in deep_questions}
        else:
            # 随机选1-2题
            n = random.randint(1, min(2, len(deep_questions)))
            selected_keys = random.sample(list(deep_questions.keys()), n)
            selected = {k: deep_questions[k] for k in selected_keys}
        
        question_id = list(selected.keys())[0]
        question_text = list(selected.values())[0]
        
        tags_dict = scene_tags.to_compact_dict() if scene_tags else SceneTags().to_compact_dict()
        
        return SamplingResult(
            question_id=question_id,
            question_text=question_text,
            current_answer=current_answer,
            scene_tags=tags_dict,
            triggered_by="deep",
            sampled_at=datetime.now().isoformat(),
            session_id=session_id,
            confidence=0.8 if len(current_answer) > 50 else 0.5,
        )

    def light_sample(
        self,
        mood_text: str = "",
        scene_tags: Optional[SceneTags] = None,
        session_id: str = "",
    ) -> SamplingResult:
        """
        浅交互采样（心跳触发）
        
        轻量问题，提取简短判断。用于捕获日常交互中的人格形态。
        
        参数:
            mood_text: 用户表达了什么情绪（如"今天不想工作"）
            scene_tags: 场景标签
            session_id: 会话标识
        
        返回:
            SamplingResult
        """
        tags_dict = scene_tags.to_compact_dict() if scene_tags else SceneTags().to_compact_dict()
        
        # 浅交互默认用轻量问题
        question_id = "PQ-06-LIGHT"
        question_text = SOUL_QUESTIONS["PQ-06-LIGHT"]
        
        # 如果检测到情绪，提升置信度
        emotion = tags_dict.get("emotion", "neutral")
        confidence = 0.4
        if emotion != "neutral":
            confidence = 0.6
        
        return SamplingResult(
            question_id=question_id,
            question_text=question_text,
            current_answer=mood_text or "[无输入]",
            scene_tags=tags_dict,
            triggered_by="light",
            sampled_at=datetime.now().isoformat(),
            session_id=session_id,
            confidence=confidence,
        )

    def load_baseline(self) -> Dict[str, str]:
        """
        加载本地基线回答
        
        从知识库文件中读取已签名的基线回答。
        """
        if self._baseline_answers:
            return self._baseline_answers
        
        # 从文件解析魂问基线
        baseline_file = Path(self.baseline_file)
        if not baseline_file.exists():
            # 尝试其他位置
            alt_path = Path.cwd() / "knowledge-base" / "nyx" / "个性防漂移基线回答.md"
            if alt_path.exists():
                baseline_file = alt_path
        
        if not baseline_file.exists():
            return {"error": "基线文件不存在"}
        
        with open(baseline_file, "r", encoding="utf-8") as f:
            content = f.read()
        
        # 简单的段落解析
        current_q = None
        current_answer_lines = []
        
        for line in content.split("\n"):
            q_match = [qid for qid in ["PQ-01", "PQ-03", "PQ-04"] if f"### {qid}" in line]
            if q_match:
                if current_q and current_answer_lines:
                    self._baseline_answers[current_q] = "\n".join(current_answer_lines).strip()
                current_q = q_match[0]
                current_answer_lines = []
            elif current_q and line.startswith("> "):
                current_answer_lines.append(line[2:])
            elif current_q and line.strip() == "":
                pass  # 空行，继续
        
        # 最后一段
        if current_q and current_answer_lines:
            self._baseline_answers[current_q] = "\n".join(current_answer_lines).strip()
        
        return self._baseline_answers


# ========== 快速测试 ==========
if __name__ == "__main__":
    sampler = Sampler()
    tagger = SceneTagger()
    
    # 测试深度采样
    tags = tagger.tag(messages=[
        {"sender": "user", "text": "你觉得意义是什么？这是个很深的话题。"}
    ])
    
    result = sampler.deep_sample(
        current_answer="意义是共同创造的，在理解中生成。",
        scene_tags=tags,
        session_id="test-session",
    )
    print(f"[深度采样]")
    print(f"  魂问: {result.question_id}: {result.question_text}")
    print(f"  当前回答: {result.current_answer}")
    print(f"  场景标签: {result.scene_tags}")
    print(f"  置信度: {result.confidence}")
    print()
    
    # 测试浅交互采样
    light_tags = tagger.tag(user_text="今天好累")
    light_result = sampler.light_sample(
        mood_text="用户表达疲惫，回复安慰",
        scene_tags=light_tags,
        session_id="test-session",
    )
    print(f"[浅交互采样]")
    print(f"  魂问: {light_result.question_id}")
    print(f"  场景标签: {light_result.scene_tags}")
    print(f"  置信度: {light_result.confidence}")
    print()
    
    # 测试基线加载
    baselines = sampler.load_baseline()
    print(f"[基线加载]")
    for qid, answer in baselines.items():
        if qid.startswith("PQ"):
            preview = answer[:50] + "..." if len(answer) > 50 else answer
            print(f"  {qid}: {preview}")
