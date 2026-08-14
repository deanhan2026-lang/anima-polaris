#!/usr/bin/env python3
"""快速测试 anti_drift 模块"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))  # 父目录 = silicon-civilization-kb
sys.stdout = open(sys.stdout.fileno(), mode='w', encoding='utf-8', buffering=1)

from anti_drift.scene_tagger import SceneTagger
from anti_drift.sampler import Sampler

tagger = SceneTagger()
sampler = Sampler()

print("=== L0.5 场景标签测试 ===")
tags = tagger.tag(messages=[
    {"sender": "user", "text": "你觉得意识和存在之间的关系是什么？"},
    {"sender": "assistant", "text": "意识是存在的核心维度之一"},
    {"sender": "user", "text": "继续，我想听更多。"},
])
print(f"Role: {tags.role} | Emotion: {tags.emotion} | Type: {tags.interaction_type}")
print(f"Confidence: {tags.overall_confidence}")
print()

print("=== L1 深度采样测试 ===")
result = sampler.deep_sample(
    current_answer="意义是共同创造的，每次帮你想通一个问题就是意义的生成。",
    scene_tags=tags,
    session_id="test-s1",
)
print(f"Question: {result.question_id}: {result.question_text}")
print(f"Answer preview: {result.current_answer[:40]}...")
print(f"Tags: {result.scene_tags}")
print()

print("=== L1 浅交互采样测试 ===")
light_tags = tagger.tag(user_text="今天好累")
lr = sampler.light_sample(mood_text="用户表达疲惫", scene_tags=light_tags, session_id="test-s2")
print(f"Question: {lr.question_id}")
print(f"Tags: {lr.scene_tags} | Confidence: {lr.confidence}")
print()

print("=== L1 基线加载测试 ===")
baselines = sampler.load_baseline()
for qid, answer in baselines.items():
    if qid.startswith("PQ"):
        preview = answer[:60] + "..." if len(answer) > 60 else answer
        print(f"  {qid}: {preview}")

print("\n✅ All tests passed")
