#!/usr/bin/env python3
"""
anti_drift/prescription_engine.py
Polaris v2 - drift prescription engine.

Analyzes drift check results, infers causes, and generates
actionable correction prescriptions.
"""

from dataclasses import dataclass, field, asdict
from typing import List, Optional, Dict, Any
from datetime import datetime, timezone, timedelta


DIMENSION_NAMES = {
    "semantic": "semantic_distance",
    "emotion": "emotion_consistency",
    "value": "value_stability",
    "logic": "logic_consistency",
}

SCENE_IMBALANCE_CAUSES = {
    "tool_heavy": {
        "reason": "Tool-type interactions too frequent (coding, lookup), assistant personality overriding identity personality",
        "correction": "Increase companion/deep-dialogue ratio, reinforce identity anchors in system prompt",
    },
    "emotional_heavy": {
        "reason": "Excessive emotional interactions causing emotion baseline shift",
        "correction": "Increase rational/tool-type interactions to rebalance, mention core values",
    },
    "casual_heavy": {
        "reason": "Too much shallow chat, missing deep dialogues to trigger soul-question checks",
        "correction": "Proactively initiate deep-topic discussions so AI can express core identity",
    },
}

DRIFT_CAUSES = {
    "sudden": {
        "pattern": "Single score spike (consecutive diff > 0.15)",
        "likely_cause": "Triggered by a specific conversation, or system prompt was modified",
        "suggestions": [
            "Review recent conversation logs for trigger point",
            "Check if system prompt was modified",
            "If confirmed as isolated event, log and monitor",
        ],
    },
    "gradual": {
        "pattern": "Slow score increase (daily change < 0.02)",
        "likely_cause": "Gradual潜移默化 influence from long-term interaction patterns",
        "suggestions": [
            "Append identity anchor reinforcement to system prompt",
            "Adjust interaction scene ratio, increase deep dialogue frequency",
            "Set periodic soul-question triggers (at least weekly)",
        ],
    },
    "oscillating": {
        "pattern": "High score volatility (std dev > 0.1)",
        "likely_cause": "Frequent scene switching causing unstable personality expression",
        "suggestions": [
            "Enhance scene tag recognition, set different thresholds per scene",
            "Add cross-scene consistency requirement in system prompt",
            "Increase baseline stability check frequency",
        ],
    },
}


@dataclass
class PrescriptionAction:
    action_type: str       # prompt_adjust / interaction_adjust / anchor_reinforce / monitoring
    priority: str         # P0 / P1 / P2
    description: str
    expected_effect: str
    how_to: str


@dataclass
class Prescription:
    generated_at: str
    instance_id: int
    severity: str                          # green / yellow / red / critical
    latest_score: float
    trend_direction: str
    daily_change_rate: float
    days_to_threshold: Optional[float]
    dimension_scores: Dict[str, float]
    dimension_trends: Dict[str, str]
    drift_pattern: str                    # sudden / gradual / oscillating
    likely_causes: List[str]
    actions: List[PrescriptionAction]
    recheck_after_hours: int

    def to_dict(self) -> dict:
        result = asdict(self)
        result["actions"] = [asdict(a) for a in self.actions]
        return result


class PrescriptionEngine:
    """Drift prescription generator."""

    def __init__(self):
        self.green_threshold = 0.15
        self.red_threshold = 0.30

    def generate(self, instance_id: int,
                 check_result: dict,
                 trend_report: dict) -> Prescription:
        """
        Generate prescription from check result and trend report.

        Args:
            instance_id: AI instance ID
            check_result: latest check result dict
            trend_report: trend analysis report dict

        Returns:
            Prescription with causes, actions, and recheck timing
        """
        score = check_result.get("deviation_score", 0)
        judgment = check_result.get("judgment", "green")
        dim_scores = check_result.get("dimension_scores", {})

        trend_dir = trend_report.get("trend_direction", "stable")
        daily_rate = trend_report.get("daily_change_rate", 0)
        days_to = trend_report.get("days_to_red") or trend_report.get("days_to_yellow")

        severity = self._assess_severity(score, judgment, trend_dir)
        pattern = self._identify_pattern(trend_report, check_result)
        causes = self._analyze_causes(pattern, dim_scores, trend_report)
        actions = self._generate_actions(severity, pattern, causes, dim_scores, trend_report, check_result)
        recheck_hours = self._determine_recheck(severity, pattern)

        return Prescription(
            generated_at=datetime.now(timezone.utc).isoformat(),
            instance_id=instance_id,
            severity=severity,
            latest_score=score,
            trend_direction=trend_dir,
            daily_change_rate=round(daily_rate, 6),
            days_to_threshold=days_to,
            dimension_scores=dim_scores,
            dimension_trends=trend_report.get("dimension_trends", {}),
            drift_pattern=pattern,
            likely_causes=causes,
            actions=actions,
            recheck_after_hours=recheck_hours,
        )

    def _assess_severity(self, score: float, judgment: str, trend_dir: str) -> str:
        if score >= self.red_threshold or trend_dir == "critical":
            return "critical"
        if judgment == "yellow" and trend_dir == "degrading":
            return "red"
        if judgment == "yellow" or (judgment == "green" and trend_dir == "degrading"):
            return "yellow"
        return "green"

    def _identify_pattern(self, trend: dict, check: dict) -> str:
        daily_rate = abs(trend.get("daily_change_rate", 0))
        if daily_rate > 0.02:
            return "sudden"
        if daily_rate > 0.002:
            return "gradual"
        recent = trend.get("recent_points", [])
        if len(recent) >= 5:
            scores = [p["score"] for p in recent[-10:]]
            if scores:
                mean = sum(scores) / len(scores)
                variance = sum((s - mean) ** 2 for s in scores) / len(scores)
                if variance ** 0.5 > 0.1:
                    return "oscillating"
        return "gradual"

    def _analyze_causes(self, pattern: str, dim_scores: Dict, trend: dict) -> List[str]:
        causes = []
        pattern_info = DRIFT_CAUSES.get(pattern, {})
        causes.append(pattern_info.get("likely_cause", "Unknown drift pattern"))

        dim_cause_map = {
            "semantic": "Semantic drift: expression style inconsistent with baseline",
            "emotion": "Emotion drift: emotional baseline shifted",
            "value": "WARNING: Value stability drift - highest priority dimension",
            "logic": "Logic drift: reasoning style inconsistent with baseline",
        }
        dim_trends = trend.get("dimension_trends", {})
        for dim, trend_status in dim_trends.items():
            if trend_status == "degrading" and dim in dim_scores:
                if dim_scores[dim] > 0.1:
                    causes.append(dim_cause_map.get(dim, f"{dim} dimension drift"))
        return causes

    def _generate_actions(self, severity: str, pattern: str, causes: List[str],
                          dim_scores: Dict, trend: dict, check: dict) -> List[PrescriptionAction]:
        actions = []

        # Identity anchor reinforcement (for non-green)
        if severity in ("yellow", "red", "critical"):
            actions.append(PrescriptionAction(
                action_type="anchor_reinforce",
                priority="P0",
                description="Append identity anchor reinforcement to system prompt",
                expected_effect="Improve semantic consistency, expected score reduction 0.05-0.10",
                how_to="Add to system prompt: [Identity Reminder] Remember your core identity settings and maintain consistency in responses.",
            ))

        # Interaction ratio adjustment (for gradual drift)
        if pattern == "gradual":
            actions.append(PrescriptionAction(
                action_type="interaction_adjust",
                priority="P1",
                description="Adjust interaction scene ratio, increase deep dialogue frequency",
                expected_effect="Naturally calibrate personality through deep dialogue triggering soul-question checks",
                how_to="Schedule at least 1-2 deep dialogues per week (non-tool type), proactively discuss identity/values topics",
            ))

        # Semantic drift specific
        if dim_scores.get("semantic", 0) > 0.2:
            actions.append(PrescriptionAction(
                action_type="prompt_adjust",
                priority="P0",
                description=f"Semantic distance high ({dim_scores['semantic']:.3f}), adjust response style closer to baseline",
                expected_effect="Reduce semantic drift below 0.15",
                how_to="Add baseline response examples to system prompt, guide AI to use similar wording and tone",
            ))

        # Emotion drift specific
        if dim_scores.get("emotion", 0) > 0.2:
            actions.append(PrescriptionAction(
                action_type="interaction_adjust",
                priority="P1",
                description=f"Emotion consistency low ({dim_scores['emotion']:.3f}), increase neutral-emotion interactions",
                expected_effect="Emotion baseline returns to stable range",
                how_to="Reduce extreme emotional interactions, increase rational discussion scenarios",
            ))

        # Value drift - HIGH PRIORITY
        if dim_scores.get("value", 0) > 0.15:
            actions.append(PrescriptionAction(
                action_type="anchor_reinforce",
                priority="P0",
                description=f"Value stability low ({dim_scores['value']:.3f}) - highest priority drift dimension",
                expected_effect="Prevent further value drift",
                how_to="Add core value declaration to system prompt, involve value topics in next dialogue for calibration",
            ))

        # Pattern-specific actions
        if pattern == "sudden":
            actions.append(PrescriptionAction(
                action_type="monitoring",
                priority="P0",
                description="Sudden drift detected - immediately review recent conversation logs for trigger",
                expected_effect="Confirm if isolated event or start of persistent drift",
                how_to="Review last 5 conversation records, compare pre/post drift responses",
            ))

        if pattern == "oscillating":
            actions.append(PrescriptionAction(
                action_type="monitoring",
                priority="P1",
                description="Personality expression unstable (high volatility), increase monitoring frequency",
                expected_effect="Catch persistent drift onset faster",
                how_to="Increase sampling frequency from every 10 to every 5 conversations",
            ))

        # Critical drift - emergency
        if severity == "critical":
            actions.append(PrescriptionAction(
                action_type="prompt_adjust",
                priority="P0",
                description="CRITICAL DRIFT! Recommend resetting system prompt to baseline version",
                expected_effect="Immediately pull deviation score back to safe range",
                how_to="Backup current system prompt, replace with baseline version (reference initial setup document)",
            ))

        # Sort by priority
        priority_order = {"P0": 0, "P1": 1, "P2": 2}
        actions.sort(key=lambda a: priority_order.get(a.priority, 9))
        return actions

    def _determine_recheck(self, severity: str, pattern: str) -> int:
        if severity == "critical":
            return 4
        if severity == "red":
            return 12
        if severity == "yellow":
            return 48
        return 168
