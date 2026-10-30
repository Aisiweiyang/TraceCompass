"""Interest 评分:纯函数(inference-design v1.3 §2)。

所有分量归一到 [0,1];公式与常数在此集中,便于表驱动单测与回归。
obligation 不进加权评分,只用于 interest_type 分类与展示。
时间型分量(recency/persistence/return_rate)依赖时间戳子集;完全无时间戳时置 None。
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta

FOLLOWUP_PATTERN = re.compile(r"[??]|还有|然后|具体|深入|更|how|why|what if", re.IGNORECASE)
OBLIGATION_PATTERN = re.compile(
    r"deadline|due|交付|上线前|客户|甲方|老板|领导|布置|安排|作业|必须|赶|周五前|周一前|KPI|考核",
    re.IGNORECASE,
)


@dataclass
class TopicSignals:
    """一个 topic 的原始统计(raw_stats 的来源)。时间字段可为 None。"""

    tokens_total: int
    active_weeks: int                     # 有活动的自然周数(时间戳子集)
    span_weeks: int                       # 首末活动跨度的周数(向上取整)
    distinct_sessions: int
    mean_session_touch_msgs: float        # 触及该 topic 的会话中,平均触及消息数
    mean_chunk_tokens: float
    user_msgs: int
    followup_hits: int
    obligation_hits: int
    co_occurring_topics: int
    linked_active_projects: int           # 0/1(存在 active project)
    days_since_last: float | None         # 距 now
    weekly_counts: dict[str, int] = field(default_factory=dict)  # 周键 -> 活动消息数
    temporal_missing: bool = False        # 无任何时间戳


@dataclass
class InterestComponents:
    frequency: float
    recency: float | None
    persistence: float | None
    depth: float
    return_rate: float | None
    followup_density: float
    project_conversion: float
    connectivity: float
    voluntary: float
    obligation: float


def persistence_score(signals: TopicSignals) -> float | None:
    """0.7×(active/max(4,span)) + 0.3×min(span,52)/52。

    短爆量数学上不可能得满分: 一周爆量 → 0.7×(1/4)+0.3×(1/52) ≈ 0.18。
    """
    if signals.temporal_missing:
        return None
    denom = max(4, signals.span_weeks)
    return 0.7 * (signals.active_weeks / denom) + 0.3 * (min(signals.span_weeks, 52) / 52)


def recency_score(signals: TopicSignals, half_life_days: float) -> float | None:
    if signals.temporal_missing or signals.days_since_last is None:
        return None
    return math.exp(-signals.days_since_last / max(1.0, half_life_days))


def return_rate_score(signals: TopicSignals) -> float | None:
    if signals.temporal_missing:
        return None
    return min(1.0, signals.distinct_sessions / max(1, signals.active_weeks))


def depth_score(signals: TopicSignals) -> float:
    touch = min(1.0, signals.mean_session_touch_msgs / 10.0)
    tokens = min(1.0, signals.mean_chunk_tokens / 400.0)
    return math.sqrt(touch * tokens)


def followup_density_score(signals: TopicSignals) -> float:
    return min(1.0, signals.followup_hits / max(1, signals.user_msgs))


def obligation_score(signals: TopicSignals) -> float:
    return min(1.0, signals.obligation_hits / max(1, signals.user_msgs))


def frequency_score(signals: TopicSignals, max_tokens_over_topics: int) -> float:
    denom = math.log1p(max(max_tokens_over_topics, 1))
    return math.log1p(signals.tokens_total) / denom if denom > 0 else 0.0


def connectivity_score(signals: TopicSignals, max_co_over_topics: int) -> float:
    denom = math.log1p(max(max_co_over_topics, 1))
    return math.log1p(signals.co_occurring_topics) / denom if denom > 0 else 0.0


def project_conversion_score(signals: TopicSignals) -> float:
    if signals.linked_active_projects >= 1:
        return 1.0
    return 0.0  # V0.1: 无候选/inferred project 关联(M4 后由 entities 回填)


def compute_components(
    signals: TopicSignals,
    max_tokens_over_topics: int,
    max_co_over_topics: int,
    half_life_days: float,
) -> InterestComponents:
    return InterestComponents(
        frequency=frequency_score(signals, max_tokens_over_topics),
        recency=recency_score(signals, half_life_days),
        persistence=persistence_score(signals),
        depth=depth_score(signals),
        return_rate=return_rate_score(signals),
        followup_density=followup_density_score(signals),
        project_conversion=project_conversion_score(signals),
        connectivity=connectivity_score(signals, max_co_over_topics),
        obligation=obligation_score(signals),
        voluntary=1.0 - obligation_score(signals),
    )


def weighted_score(
    components: InterestComponents, weights: dict[str, float]
) -> float:
    """加权和;缺失(None)分量的权重在剩余分量中重归一。"""
    values: dict[str, float] = {
        "frequency": components.frequency,
        "recency": components.recency if components.recency is not None else 0.0,
        "persistence": components.persistence
        if components.persistence is not None
        else 0.0,
        "depth": components.depth,
        "return_rate": components.return_rate if components.return_rate is not None else 0.0,
        "followup_density": components.followup_density,
        "project_conversion": components.project_conversion,
        "connectivity": components.connectivity,
        "voluntary": components.voluntary,
    }
    missing = {
        name for name, value in (
            ("recency", components.recency),
            ("persistence", components.persistence),
            ("return_rate", components.return_rate),
        ) if value is None
    }
    active_weights = {k: w for k, w in weights.items() if k not in missing and k in values}
    total_weight = sum(active_weights.values())
    if total_weight <= 0:
        return 0.0
    return sum(values[k] * w for k, w in active_weights.items()) / total_weight


# ── 生命周期(规则按序命中即停,顺序即优先级;inference-design §3)────────────


def _week_key(dt: datetime) -> str:
    iso = dt.isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"


def _activity_in_window(
    weekly_counts: dict[str, int], now: datetime, start_days: int, end_days: int
) -> int:
    """[now-end_days, now-start_days) 窗口内的活动消息总数。"""
    total = 0
    for offset in range(end_days, start_days, -7):
        key = _week_key(now - timedelta(days=offset))
        total += weekly_counts.get(key, 0)
    return total


def had_long_gap_then_recent(
    signals: TopicSignals, now: datetime, gap_days: float, recent_days: float
) -> bool:
    """存在 ≥gap_days 的静默间隙,且间隙后最近 recent_days 内重新出现。"""
    if signals.temporal_missing:
        return False
    weeks = sorted(signals.weekly_counts)
    if len(weeks) < 2:
        return False
    gaps: list[float] = []
    prev_end: datetime | None = None
    for week in weeks:
        year, week_no = week.split("-W")
        monday = datetime.fromisocalendar(int(year), int(week_no), 1)
        if prev_end is not None:
            gaps.append((monday - prev_end).days)
        prev_end = monday + timedelta(days=6)
    has_gap = any(g >= gap_days for g in gaps)
    return (
        has_gap
        and signals.days_since_last is not None
        and signals.days_since_last <= recent_days
    )


def lifecycle_of(
    signals: TopicSignals,
    cfg_lifecycle: object,
    now: datetime,
) -> str | None:
    """返回生命周期或 None(不分类)。规则顺序 = 优先级。"""
    temporary_max_span = getattr(cfg_lifecycle, "temporary_max_span", 14.0)
    temporary_max_depth = getattr(cfg_lifecycle, "temporary_max_depth", 0.4)
    emerging_window = getattr(cfg_lifecycle, "emerging_window", 60.0)
    emerging_min_persistence = getattr(cfg_lifecycle, "emerging_min_persistence", 0.3)
    dormant_min_gap = getattr(cfg_lifecycle, "dormant_min_gap", 90.0)
    dormant_min_persistence = getattr(cfg_lifecycle, "dormant_min_persistence", 0.5)
    stable_min_months = getattr(cfg_lifecycle, "stable_min_months", 3.0)
    stable_min_active_ratio = getattr(cfg_lifecycle, "stable_min_active_ratio", 0.25)
    reactivated_recent = getattr(cfg_lifecycle, "reactivated_recent_days", 30.0)

    persistence = persistence_score(signals) or 0.0
    depth = depth_score(signals)

    if had_long_gap_then_recent(signals, now, dormant_min_gap, reactivated_recent):
        return "reactivated"
    if (
        not signals.temporal_missing
        and signals.span_weeks * 7 <= temporary_max_span
        and signals.days_since_last is not None
        and signals.days_since_last > 30
        and depth < temporary_max_depth
    ):
        return "temporary"
    if (
        not signals.temporal_missing
        and signals.days_since_last is not None
        and signals.days_since_last <= emerging_window
        and _recent_slope_positive(signals, now)
        and persistence >= emerging_min_persistence
    ):
        return "emerging"
    if (
        not signals.temporal_missing
        and signals.days_since_last is not None
        and signals.days_since_last >= dormant_min_gap
        and persistence >= dormant_min_persistence
    ):
        return "dormant"
    if (
        not signals.temporal_missing
        and signals.span_weeks / 4.33 >= stable_min_months
        and signals.active_weeks / max(1, signals.span_weeks) >= stable_min_active_ratio
    ):
        return "stable"
    if not signals.temporal_missing and signals.days_since_last is not None \
            and signals.days_since_last <= emerging_window:
        return "emerging"  # 新出现但数据不足:归 emerging 并由 confidence 低反映
    return None


def _recent_slope_positive(signals: TopicSignals, now: datetime) -> bool:
    """最近 4 周活动量 > 之前 4 周活动量。"""
    recent = _activity_in_window(signals.weekly_counts, now, 0, 28)
    prior = _activity_in_window(signals.weekly_counts, now, 28, 56)
    return recent > prior


def interest_type_of(
    lifecycle: str | None, components: InterestComponents
) -> str:
    """按序命中即停;灰色地带诚实标 unknown。"""
    if lifecycle == "temporary":
        return "temporary"
    if components.obligation >= 0.5:
        return "professional"
    if (
        components.obligation < 0.5
        and components.project_conversion >= 0.5
        and components.depth < 0.5
    ):
        return "instrumental"
    if (
        components.obligation < 0.2
        and (components.persistence or 0.0) >= 0.6
        and components.depth >= 0.5
    ):
        return "intrinsic"
    return "unknown"


def confidence_of(
    evidence_count: int, saturation: int, temporal_missing: bool, method: str = "statistical"
) -> float:
    credibility = {"deterministic": 0.9, "statistical": 0.7}.get(method, 0.5)
    confidence = credibility * min(1.0, evidence_count / max(1, saturation))
    if temporal_missing:
        confidence *= 0.6
    return round(confidence, 3)
