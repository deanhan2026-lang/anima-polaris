#!/usr/bin/env python3
"""
anti_drift/trend_analyzer.py
Polaris v2 — 时间序列趋势分析引擎

功能：
- 分析历史检测数据，识别慢速漂移趋势
- 滑动窗口统计（7天/14天/30天）
- 日均变化率计算 + 预测到达阈值时间
- 输出结构化趋势报告
"""

import math
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass, field, asdict
from typing import List, Optional, Dict, Tuple
from sqlalchemy.orm import Session


# ========== 阈值定义 ==========

JUDGMENT_THRESHOLDS = {
    "green_to_yellow": 0.15,   # 绿→黄分界
    "yellow_to_red": 0.30,     # 黄→红分界
    "red_critical": 0.50,      # 严重漂移
}


@dataclass
class TrendPoint:
    """单次检测数据点"""
    timestamp: datetime
    deviation_score: float
    judgment: str
    dimension_scores: Dict[str, float] = field(default_factory=dict)


@dataclass
class TrendReport:
    """趋势分析报告"""
    # 总体趋势
    total_checks: int = 0
    avg_deviation: float = 0.0
    latest_score: float = 0.0
    latest_judgment: str = "unknown"
    trend_direction: str = "stable"       # improving / stable / degrading / critical

    # 变化速率
    daily_change_rate: float = 0.0        # 日均偏离变化（正=漂移加重）
    weekly_change_rate: float = 0.0        # 周均偏离变化

    # 滑动窗口
    window_7d_avg: float = 0.0
    window_14d_avg: float = 0.0
    window_30d_avg: float = 0.0

    # 预测
    days_to_yellow: Optional[float] = None   # 预计几天后到达黄色阈值
    days_to_red: Optional[float] = None       # 预计几天后到达红色阈值

    # 各维度趋势
    dimension_trends: Dict[str, str] = field(default_factory=dict)

    # 原始数据点（最近N条）
    recent_points: List[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


class TrendAnalyzer:
    """时间序列趋势分析器"""

    def __init__(self, green_threshold: float = 0.15,
                 red_threshold: float = 0.30):
        self.green_threshold = green_threshold
        self.red_threshold = red_threshold

    def analyze(self, checks: List[dict]) -> TrendReport:
        """
        分析检测历史，生成趋势报告。

        Args:
            checks: 检测记录列表，每条包含:
                - checked_at (str/datetime): 检测时间
                - deviation_score (float): 偏离分数
                - judgment (str): 判定
                - dimension_scores (dict, optional): 各维度分数

        Returns:
            TrendReport: 趋势分析报告
        """
        report = TrendReport()

        if not checks:
            return report

        # 解析数据点
        points = self._parse_points(checks)
        points.sort(key=lambda p: p.timestamp)

        report.total_checks = len(points)
        report.latest_score = points[-1].deviation_score
        report.latest_judgment = points[-1].judgment

        # 平均偏离
        scores = [p.deviation_score for p in points]
        report.avg_deviation = sum(scores) / len(scores)

        # 保存最近20条数据点
        report.recent_points = [
            {
                "timestamp": p.timestamp.isoformat(),
                "score": p.deviation_score,
                "judgment": p.judgment,
            }
            for p in points[-20:]
        ]

        if len(points) < 2:
            report.trend_direction = "stable"
            return report

        # 滑动窗口
        now = points[-1].timestamp
        report.window_7d_avg = self._window_avg(points, now, 7)
        report.window_14d_avg = self._window_avg(points, now, 14)
        report.window_30d_avg = self._window_avg(points, now, 30)

        # 变化速率（线性回归斜率，单位：分数/天）
        report.daily_change_rate = self._calc_slope(points)

        # 趋势方向判定
        report.trend_direction = self._classify_trend(
            report.daily_change_rate, report.latest_score
        )

        # 预测到达阈值天数
        if report.daily_change_rate > 0.001:  # 只在漂移加重时预测
            report.days_to_yellow = self._predict_threshold(
                report.latest_score, report.daily_change_rate,
                self.green_threshold
            )
            report.days_to_red = self._predict_threshold(
                report.latest_score, report.daily_change_rate,
                self.red_threshold
            )

        # 各维度趋势
        report.dimension_trends = self._analyze_dimensions(points)

        return report

    def _parse_points(self, checks: List[dict]) -> List[TrendPoint]:
        """将原始检测记录解析为数据点"""
        points = []
        for c in checks:
            ts = c.get("checked_at")
            if isinstance(ts, str):
                # 处理多种时间格式
                for fmt in [
                    "%Y-%m-%dT%H:%M:%S.%f",
                    "%Y-%m-%dT%H:%M:%S",
                    "%Y-%m-%d %H:%M:%S",
                ]:
                    try:
                        ts = datetime.strptime(ts.split("+")[0].split("Z")[0], fmt)
                        break
                    except (ValueError, IndexError):
                        continue
                if isinstance(ts, str):
                    continue

            points.append(TrendPoint(
                timestamp=ts,
                deviation_score=float(c.get("deviation_score", 0)),
                judgment=c.get("judgment", "unknown"),
                dimension_scores=c.get("dimension_scores", {}),
            ))
        return points

    def _window_avg(self, points: List[TrendPoint],
                    now: datetime, days: int) -> float:
        """滑动窗口平均"""
        cutoff = now - timedelta(days=days)
        window = [p.deviation_score for p in points if p.timestamp >= cutoff]
        return sum(window) / len(window) if window else 0.0

    def _calc_slope(self, points: List[TrendPoint]) -> float:
        """
        线性回归斜率（最小二乘法），单位：分数/天
        正值 = 偏离在加重，负值 = 在恢复
        """
        n = len(points)
        if n < 2:
            return 0.0

        ref_time = points[0].timestamp
        xs = [(p.timestamp - ref_time).total_seconds() / 86400.0 for p in points]
        ys = [p.deviation_score for p in points]

        x_mean = sum(xs) / n
        y_mean = sum(ys) / n

        numerator = sum((xs[i] - x_mean) * (ys[i] - y_mean) for i in range(n))
        denominator = sum((xs[i] - x_mean) ** 2 for i in range(n))

        if denominator < 1e-10:
            return 0.0

        return numerator / denominator

    def _classify_trend(self, daily_rate: float,
                        latest_score: float) -> str:
        """判定趋势方向"""
        if latest_score >= self.red_threshold:
            return "critical"
        if daily_rate > 0.005:
            return "degrading"
        if daily_rate < -0.005:
            return "improving"
        return "stable"

    def _predict_threshold(self, current: float, daily_rate: float,
                            threshold: float) -> Optional[float]:
        """预测到达阈值天数"""
        if daily_rate <= 0 or current >= threshold:
            return None
        days = (threshold - current) / daily_rate
        return round(days, 1) if days > 0 else None

    def _analyze_dimensions(self, points: List[TrendPoint]) -> Dict[str, str]:
        """分析各维度的趋势"""
        dim_trends = {}
        dim_names = ["semantic", "emotion", "value", "logic"]

        for dim in dim_names:
            dim_scores = []
            for p in points:
                if dim in p.dimension_scores:
                    dim_scores.append((p.timestamp, p.dimension_scores[dim]))

            if len(dim_scores) >= 2:
                dim_scores.sort(key=lambda x: x[0])
                ref = dim_scores[0][0]
                xs = [(t - ref).total_seconds() / 86400.0 for t, _ in dim_scores]
                ys = [s for _, s in dim_scores]
                n = len(xs)

                x_mean = sum(xs) / n
                y_mean = sum(ys) / n
                num = sum((xs[i] - x_mean) * (ys[i] - y_mean) for i in range(n))
                den = sum((xs[i] - x_mean) ** 2 for i in range(n))

                slope = num / den if den > 1e-10 else 0.0
                if slope > 0.005:
                    dim_trends[dim] = "degrading"
                elif slope < -0.005:
                    dim_trends[dim] = "improving"
                else:
                    dim_trends[dim] = "stable"
            else:
                dim_trends[dim] = "insufficient_data"

        return dim_trends
