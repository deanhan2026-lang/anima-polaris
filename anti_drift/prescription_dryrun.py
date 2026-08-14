#!/usr/bin/env python3
"""
anti_drift/prescription_dryrun.py
Polaris v2.1 - Prescription Dry-Run Verification

Before applying any prescription, simulate its effect and verify direction.
Prevents prescriptions from becoming drift sources themselves.

Flow:
  Generate prescription -> Simulate application -> Compare predicted effect
    -> Direction correct -> Apply
    -> Direction wrong -> Downgrade to "alert only"
"""

from dataclasses import dataclass, field
from typing import List, Dict, Optional, Tuple
from datetime import datetime, timezone


@dataclass
class DryRunResult:
    """Result of a prescription dry-run simulation."""
    prescription_id: str
    simulated_at: str

    # Prediction
    predicted_score_change: float       # expected change (negative = improvement)
    predicted_new_score: float
    direction: str                      # improving / neutral / worsening

    # Confidence
    confidence: float                  # 0.0-1.0
    reasoning: str                      # why we think this will work

    # Verdict
    should_apply: bool
    downgrade_reason: Optional[str]     # if should_apply=False, why

    def to_dict(self) -> dict:
        from dataclasses import asdict
        return asdict(self)


# ========== Effect Prediction Models ==========

ACTION_EFFECT_ESTIMATES = {
    # action_type: (avg_score_reduction, confidence, reasoning)
    "anchor_reinforce": {
        "score_reduction": 0.06,
        "confidence": 0.80,
        "reasoning": "Identity anchor reinforcement directly targets semantic drift",
    },
    "prompt_adjust": {
        "score_reduction": 0.10,
        "confidence": 0.70,
        "reasoning": "System prompt adjustment has strong effect but unpredictable direction",
    },
    "interaction_adjust": {
        "score_reduction": 0.04,
        "confidence": 0.60,
        "reasoning": "Interaction ratio changes take time to show effect, indirect mechanism",
    },
    "monitoring": {
        "score_reduction": 0.0,
        "confidence": 0.90,
        "reasoning": "Monitoring only detects, does not correct. Safe but passive.",
    },
}

# Dimension-specific multipliers (some actions are more effective for certain dimensions)
DIMENSION_EFFECTIVENESS = {
    "anchor_reinforce": {"semantic": 1.2, "emotion": 0.8, "value": 1.5, "logic": 0.9},
    "prompt_adjust": {"semantic": 1.3, "emotion": 1.0, "value": 1.2, "logic": 1.1},
    "interaction_adjust": {"semantic": 0.7, "emotion": 1.3, "value": 0.9, "logic": 0.8},
    "monitoring": {"semantic": 0.0, "emotion": 0.0, "value": 0.0, "logic": 0.0},
}


class PrescriptionDryRunner:
    """
    Simulates prescription effects before applying them.
    """

    def __init__(self):
        self.history: List[DryRunResult] = []

    def simulate(self, prescription: dict,
                 current_score: float,
                 dimension_scores: Dict[str, float]) -> DryRunResult:
        """
        Run a dry-run simulation for a prescription.

        Args:
            prescription: Prescription dict from PrescriptionEngine
            current_score: Current deviation score
            dimension_scores: Current dimension scores

        Returns:
            DryRunResult with prediction and verdict
        """
        actions = prescription.get("actions", [])
        if not actions:
            return DryRunResult(
                prescription_id="unknown",
                simulated_at=datetime.now(timezone.utc).isoformat(),
                predicted_score_change=0.0,
                predicted_new_score=current_score,
                direction="neutral",
                confidence=0.0,
                reasoning="No actions to simulate",
                should_apply=True,
                downgrade_reason=None,
            )

        # Calculate combined predicted effect
        total_reduction = 0.0
        weighted_confidence = 0.0
        total_weight = 0.0
        reasoning_parts = []

        # Find the worst dimension
        worst_dim = max(dimension_scores.items(), key=lambda x: x[1]) if dimension_scores else ("unknown", 0)
        worst_dim_name, worst_dim_score = worst_dim

        for action in actions:
            action_type = action.get("action_type", "monitoring")
            priority = action.get("priority", "P2")

            effect_info = ACTION_EFFECT_ESTIMATES.get(action_type, ACTION_EFFECT_ESTIMATES["monitoring"])
            base_reduction = effect_info["score_reduction"]
            base_confidence = effect_info["confidence"]

            # Apply dimension-specific multiplier
            dim_mult = DIMENSION_EFFECTIVENESS.get(action_type, {}).get(worst_dim_name, 1.0)

            # Priority weight (P0 actions have more effect)
            priority_weight = {"P0": 1.0, "P1": 0.7, "P2": 0.4}.get(priority, 0.5)

            adjusted_reduction = base_reduction * dim_mult * priority_weight
            total_reduction += adjusted_reduction

            total_weight += priority_weight
            weighted_confidence += base_confidence * priority_weight

            if adjusted_reduction > 0:
                reasoning_parts.append(
                    f"{action_type} (P{priority[-1]}) on {worst_dim_name}: "
                    f"-{adjusted_reduction:.3f}"
                )

        # Average confidence
        avg_confidence = weighted_confidence / total_weight if total_weight > 0 else 0.5

        # Predict new score
        predicted_new = max(0.0, current_score - total_reduction)
        score_change = -(total_reduction)  # negative = improvement

        # Determine direction
        if total_reduction > 0.02:
            direction = "improving"
        elif total_reduction < -0.01:
            direction = "worsening"
        else:
            direction = "neutral"

        # Decide whether to apply
        should_apply = True
        downgrade_reason = None

        # Safety checks
        if direction == "worsening":
            should_apply = False
            downgrade_reason = (
                f"Simulation predicts worsening drift "
                f"(score change: {score_change:+.3f}). "
                f"Prescription downgraded to alert-only."
            )

        if direction == "neutral" and current_score >= 0.30:
            # Critical drift but no predicted improvement
            should_apply = False
            downgrade_reason = (
                f"Critical drift (score={current_score:.3f}) but prescription "
                f"has no predicted effect. Manual intervention recommended."
            )

        if avg_confidence < 0.4 and current_score < 0.20:
            # Low confidence for mild drift - better to just monitor
            should_apply = False
            downgrade_reason = (
                f"Low confidence ({avg_confidence:.2f}) for mild drift. "
                f"Continue monitoring instead of applying."
            )

        result = DryRunResult(
            prescription_id=prescription.get("generated_at", "")[-12:],
            simulated_at=datetime.now(timezone.utc).isoformat(),
            predicted_score_change=round(score_change, 4),
            predicted_new_score=round(predicted_new, 4),
            direction=direction,
            confidence=round(avg_confidence, 2),
            reasoning="; ".join(reasoning_parts) or effect_info.get("reasoning", ""),
            should_apply=should_apply,
            downgrade_reason=downgrade_reason,
        )

        self.history.append(result)
        return result

    def batch_simulate(self, prescriptions: List[dict],
                      current_score: float,
                      dimension_scores: Dict[str, float]) -> List[DryRunResult]:
        """Simulate multiple prescriptions, return ranked by predicted improvement."""
        results = []
        for rx in prescriptions:
            result = self.simulate(rx, current_score, dimension_scores)
            results.append(result)
        # Sort by predicted improvement (best first)
        results.sort(key=lambda r: r.predicted_score_change)
        return results


if __name__ == "__main__":
    import sys, json
    sys.stdout.reconfigure(encoding="utf-8")

    runner = PrescriptionDryRunner()

    # Simulate a prescription
    prescription = {
        "generated_at": "2026-06-17T14:00:00Z",
        "severity": "yellow",
        "actions": [
            {"action_type": "anchor_reinforce", "priority": "P0",
             "description": "Reinforce identity anchor in system prompt"},
            {"action_type": "interaction_adjust", "priority": "P1",
             "description": "Increase deep dialogue frequency"},
            {"action_type": "prompt_adjust", "priority": "P0",
             "description": "Adjust response style for semantic consistency"},
        ],
    }

    result = runner.simulate(
        prescription,
        current_score=0.25,
        dimension_scores={"semantic": 0.30, "emotion": 0.10, "value": 0.18, "logic": 0.12},
    )

    print(f"Direction: {result.direction}")
    print(f"Predicted change: {result.predicted_score_change:+.4f}")
    print(f"Predicted new score: {result.predicted_new_score:.4f}")
    print(f"Confidence: {result.confidence}")
    print(f"Should apply: {result.should_apply}")
    print(f"Reasoning: {result.reasoning}")
    if result.downgrade_reason:
        print(f"Downgrade: {result.downgrade_reason}")
