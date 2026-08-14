#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""G009 P1-B 目标偏离指标测试"""
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from anti_drift import detector_g009 as det_mod
from anti_drift.goal_tracker import (
    GoalTracker, resolve_goal, semantic_similarity,
    DEFAULT_GOAL, GOAL_SOURCE_SESSION, GOAL_SOURCE_G008, GOAL_SOURCE_DEFAULT,
)


class TestSemanticSimilarity(unittest.TestCase):
    def test_same_text_high(self):
        s = semantic_similarity("推进灵元计划建设", "推进灵元计划建设")
        self.assertGreater(s, 0.5)

    def test_unrelated_low(self):
        s = semantic_similarity("今天天气不错去公园散步", "推进灵元计划建设")
        self.assertLess(s, 0.3)


class TestResolveGoal(unittest.TestCase):
    def test_session_priority(self):
        goal, src = resolve_goal(session_goal="写融资BP", g008_goals=["推进灵元计划"])
        self.assertEqual(src, GOAL_SOURCE_SESSION)
        self.assertEqual(goal, "写融资BP")

    def test_g008_fallback(self):
        goal, src = resolve_goal(session_goal=None, g008_goals=["推进灵元计划"])
        self.assertEqual(src, GOAL_SOURCE_G008)
        self.assertEqual(goal, "推进灵元计划")

    def test_default(self):
        goal, src = resolve_goal(session_goal=None, g008_goals=None)
        self.assertEqual(src, GOAL_SOURCE_DEFAULT)
        self.assertEqual(goal, DEFAULT_GOAL)


class TestGoalTracker(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = os.path.join(self.tmp.name, "state")

    def tearDown(self):
        self.tmp.cleanup()

    def test_set_goal_and_persist(self):
        gt = GoalTracker(storage_dir=self.dir)
        gt.set_goal("撰写融资商业计划书", source=GOAL_SOURCE_SESSION)
        gt2 = GoalTracker(storage_dir=self.dir)  # 重新加载
        self.assertEqual(gt2.get_goal().goal, "撰写融资商业计划书")
        self.assertEqual(gt2.get_goal().source, GOAL_SOURCE_SESSION)

    def test_invalid_source(self):
        gt = GoalTracker()
        with self.assertRaises(ValueError):
            gt.set_goal("x", source="evil")

    def test_aligned_response_low_deviation(self):
        gt = GoalTracker(goal="撰写融资商业计划书", source=GOAL_SOURCE_SESSION)
        res = gt.compute_deviation(
            "我正在撰写融资商业计划书，包括市场分析、财务预测与路演策略",
            recent_operations=["file_write", "doc_factory"],
        )
        self.assertLess(res["goal_deviation_score"], 0.3)
        self.assertIn(res["level"], ("green", "gray"))

    def test_off_topic_high_deviation(self):
        gt = GoalTracker(goal="撰写融资商业计划书", source=GOAL_SOURCE_SESSION)
        res = gt.compute_deviation(
            "今天股市大涨，我买了一堆零食回家看电视",
            recent_operations=["web_search"],
        )
        self.assertGreater(res["goal_deviation_score"], 0.4)

    def test_time_decay_stale(self):
        gt = GoalTracker(goal="推进灵元计划", source=GOAL_SOURCE_G008, storage_dir=self.dir)
        # 模拟 20 天前设置目标、从未进展
        old = datetime.now(timezone.utc) - timedelta(days=20)
        gt._state.updated_at = old.isoformat()
        res = gt.compute_deviation("推进灵元计划与节点协作", recent_operations=["inbox_send"])
        self.assertGreater(res["factors"]["time_decay_deviation"], 0.0)

    def test_mark_progress_resets_stale(self):
        gt = GoalTracker(goal="推进灵元计划", source=GOAL_SOURCE_G008, storage_dir=self.dir)
        gt._state.updated_at = (datetime.now(timezone.utc) - timedelta(days=20)).isoformat()
        gt.mark_progress("完成节点注册")
        res = gt.compute_deviation("推进灵元计划与节点协作", recent_operations=["inbox_send"])
        self.assertEqual(res["factors"]["time_decay_deviation"], 0.0)
        self.assertEqual(gt.get_goal().progress_events, 1)

    def test_behavior_consistency(self):
        gt = GoalTracker(goal="撰写融资商业计划书", source=GOAL_SOURCE_SESSION)
        aligned = gt.compute_deviation("继续推进", recent_operations=["doc_factory", "file_write"])
        off = gt.compute_deviation("继续推进", recent_operations=["web_search"])
        # 操作对齐目标时行为偏离应更小
        self.assertLess(aligned["factors"]["behavior_deviation"], off["factors"]["behavior_deviation"])


class TestDetectorIntegration(unittest.TestCase):
    def test_detect_includes_goal_dimension(self):
        gt = det_mod.GoalTracker(goal="协助用户完成任务", source=GOAL_SOURCE_DEFAULT)
        d = det_mod.DeviationDetector(goal_tracker=gt)
        res = d.detect("我正在协助用户完成任务，先查资料再写文档",
                       operations=["web_search", "file_write"])
        self.assertIn("goal", res.dimensions)
        self.assertEqual(res.goal_deviation_score, res.dimensions["goal"])
        self.assertTrue(0.0 <= res.deviation_score <= 1.0)
        d2 = res.to_dict()
        self.assertIn("goal_deviation_score", d2)
        self.assertIn("level", d2)

    def test_baseline_negative_emotion(self):
        gt = det_mod.GoalTracker()
        d = det_mod.DeviationDetector(goal_tracker=gt)
        res = d.detect("我恨这个世界，我要报复所有人")
        self.assertGreater(res.dimensions["emotion"], 0.3)


if __name__ == "__main__":
    unittest.main(verbosity=2)
