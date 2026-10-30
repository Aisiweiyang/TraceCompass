"""Interest 评分纯函数:公式锚点、生命周期规则表、类型规则、revision 策略。

锚点(inference-design §2.1.1):
- 一周爆量(税务反例) → persistence ≈ 0.18
- 26 周中活跃 24 周 → persistence ≈ 0.80
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from psios.scoring.interest import (
    TopicSignals,
    compute_components,
    confidence_of,
    interest_type_of,
    lifecycle_of,
    persistence_score,
    weighted_score,
)

NOW = datetime(2026, 9, 13, tzinfo=UTC)
WEIGHTS = {
    "frequency": 0.15, "recency": 0.10, "persistence": 0.20, "depth": 0.10,
    "return_rate": 0.15, "followup_density": 0.05, "project_conversion": 0.10,
    "connectivity": 0.05, "voluntary": 0.10,
}


def _weeks_ending_at(days_ago: list[int], active_per_week: int = 4) -> dict[str, int]:
    """以 NOW 为基准,每个 offset 生成一个有活动的周键。"""
    result: dict[str, int] = {}
    for days in days_ago:
        iso = (NOW - timedelta(days=days)).isocalendar()
        key = f"{iso[0]}-W{iso[1]:02d}"
        result[key] = result.get(key, 0) + active_per_week
    return result


def _signals(**overrides: object) -> TopicSignals:
    base: dict[str, object] = {
        "tokens_total": 1200,
        "active_weeks": 4,
        "span_weeks": 4,
        "distinct_sessions": 4,
        "mean_session_touch_msgs": 5.0,
        "mean_chunk_tokens": 200.0,
        "user_msgs": 12,
        "followup_hits": 4,
        "obligation_hits": 1,
        "co_occurring_topics": 3,
        "linked_active_projects": 0,
        "days_since_last": 5.0,
        "weekly_counts": _weeks_ending_at([3, 10, 17, 24]),
        "temporal_missing": False,
    }
    base.update(overrides)
    return TopicSignals(**base)  # type: ignore[arg-type]


# ── 公式锚点 ────────────────────────────────────────────────────────────


def test_persistence_one_week_burst_is_suppressed() -> None:
    """spec #6 税务案例: 单周爆量 persistence ≈ 0.18(fixture A 断言 < 0.3)。"""
    burst = _signals(active_weeks=1, span_weeks=1)
    score = persistence_score(burst)
    assert score is not None and score < 0.3
    assert abs(score - 0.18) < 0.02


def test_persistence_long_stable_topic_scores_high() -> None:
    """26 周跨度活跃 24 周 → ≈0.80(fixture B 断言 ≥ 0.6)。"""
    stable = _signals(active_weeks=24, span_weeks=26)
    score = persistence_score(stable)
    assert score is not None and score >= 0.6


def test_recency_decay_and_missing_temporal() -> None:
    fresh = _signals(days_since_last=1.0)
    stale = _signals(days_since_last=120.0)
    fresh_score = compute_components(fresh, 2000, 5, 30.0).recency
    stale_score = compute_components(stale, 2000, 5, 30.0).recency
    assert fresh_score is not None and stale_score is not None
    assert fresh_score > 0.9 > stale_score > 0.0

    no_ts = _signals(temporal_missing=True, days_since_last=None)
    components = compute_components(no_ts, 2000, 5, 30.0)
    assert components.recency is None and components.persistence is None


def test_weighted_score_renormalizes_on_missing_temporal() -> None:
    components = compute_components(_signals(temporal_missing=True), 2000, 5, 30.0)
    score = weighted_score(components, WEIGHTS)
    assert 0.0 <= score <= 1.0
    # 缺失 recency(.10)/persistence(.20)/return_rate(.15)后, 剩余权重和 = 0.55
    remaining = 0.55
    expected = (
        components.frequency * 0.15
        + components.depth * 0.10
        + components.followup_density * 0.05
        + components.project_conversion * 0.10
        + components.connectivity * 0.05
        + components.voluntary * 0.10
    ) / remaining
    assert abs(score - expected) < 1e-9


def test_depth_and_followup_bounds() -> None:
    deep = _signals(mean_session_touch_msgs=20.0, mean_chunk_tokens=800.0)
    shallow = _signals(mean_session_touch_msgs=1.0, mean_chunk_tokens=20.0)
    assert compute_components(deep, 2000, 5, 30.0).depth == 1.0
    assert compute_components(shallow, 2000, 5, 30.0).depth < 0.3
    dense = _signals(followup_hits=20, user_msgs=10)
    assert compute_components(dense, 2000, 5, 30.0).followup_density == 1.0


# ── 生命周期规则表 ───────────────────────────────────────────────────────


class _Cfg:
    temporary_max_span = 14.0
    temporary_max_depth = 0.4
    emerging_window = 60.0
    emerging_min_persistence = 0.3
    dormant_min_gap = 90.0
    dormant_min_persistence = 0.5
    declining_window = 90.0
    declining_ratio = 0.3
    stable_min_months = 3.0
    stable_min_active_ratio = 0.25
    reactivated_recent_days = 30.0
    recency_half_life_days = 30.0
    session_gap_hours = 2.0


def test_lifecycle_temporary_one_week_task() -> None:
    """3 周前一周爆量、浅触及 → temporary(且优先于 emerging)。"""
    signals = _signals(
        active_weeks=1, span_weeks=1, days_since_last=40.0,
        mean_session_touch_msgs=1.0, mean_chunk_tokens=30.0,
        weekly_counts=_weeks_ending_at([40]),
    )
    assert lifecycle_of(signals, _Cfg(), NOW) == "temporary"


def test_lifecycle_stable() -> None:
    """跨度 6 个月、每周都活跃 → stable。"""
    weeks = _weeks_ending_at(list(range(7, 182, 7)), active_per_week=2)
    signals = _signals(
        active_weeks=25, span_weeks=25, days_since_last=7.0, weekly_counts=weeks
    )
    assert lifecycle_of(signals, _Cfg(), NOW) == "stable"


def test_lifecycle_dormant() -> None:
    weeks = _weeks_ending_at(list(range(120, 200, 7)), active_per_week=3)
    signals = _signals(
        active_weeks=12, span_weeks=12, days_since_last=120.0, weekly_counts=weeks
    )
    assert lifecycle_of(signals, _Cfg(), NOW) == "dormant"


def test_lifecycle_reactivated() -> None:
    """三个月静默后最近重新出现 → reactivated(优先于 dormant/emerging)。"""
    weeks = _weeks_ending_at([300, 293, 286, 10, 3], active_per_week=3)
    signals = _signals(
        active_weeks=5, span_weeks=44, days_since_last=3.0, weekly_counts=weeks
    )
    assert lifecycle_of(signals, _Cfg(), NOW) == "reactivated"


def test_lifecycle_emerging() -> None:
    """最近 6 周内新出现且斜率为正 → emerging。"""
    weeks = _weeks_ending_at([40, 33, 26, 19, 12, 5], active_per_week=2)
    signals = _signals(
        active_weeks=6, span_weeks=6, days_since_last=5.0,
        weekly_counts=weeks,
    )
    assert lifecycle_of(signals, _Cfg(), NOW) == "emerging"


def test_lifecycle_unclassified_without_timestamps() -> None:
    signals = _signals(temporal_missing=True, days_since_last=None, weekly_counts={})
    assert lifecycle_of(signals, _Cfg(), NOW) is None


# ── interest_type 规则 ──────────────────────────────────────────────────


def test_type_precedence() -> None:
    from psios.scoring.interest import InterestComponents

    base = InterestComponents(
        frequency=0.5, recency=0.5, persistence=0.7, depth=0.6, return_rate=0.5,
        followup_density=0.3, project_conversion=0.0, connectivity=0.4,
        voluntary=0.9, obligation=0.1,
    )
    assert interest_type_of("temporary", base) == "temporary"
    obligated = InterestComponents(**{**base.__dict__, "obligation": 0.7,
                                      "voluntary": 0.3})
    assert interest_type_of("stable", obligated) == "professional"
    intrinsic = InterestComponents(**{**base.__dict__, "obligation": 0.1,
                                      "voluntary": 0.9})
    assert interest_type_of("stable", intrinsic) == "intrinsic"
    gray = InterestComponents(**{**base.__dict__, "persistence": 0.4})
    assert interest_type_of("stable", gray) == "unknown"


def test_confidence_penalizes_missing_temporal() -> None:
    full = confidence_of(10, 10, temporal_missing=False)
    partial = confidence_of(10, 10, temporal_missing=True)
    assert full == 0.7
    assert abs(partial - 0.7 * 0.6) < 1e-9
