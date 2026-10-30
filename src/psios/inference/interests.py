"""Interest 推断 stage:topic 活动信号 → 分量评分 → 生命周期 → 落库。

- revision 只在实质变化时写(Δscore ≥ 0.05 / lifecycle / type 变化, data-model §7);
- 每个分量的代表性证据写 evidence(signal_type='signal:<分量>'),覆盖率不变量由此保证;
- 无时间戳数据: 内容型分量照常,时间型分量缺失 → 权重重归一 + confidence × 0.6。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from math import ceil

from psios.config import PsiosConfig
from psios.ids import stable_id
from psios.scoring.interest import (
    FOLLOWUP_PATTERN,
    OBLIGATION_PATTERN,
    TopicSignals,
    compute_components,
    confidence_of,
    interest_type_of,
    lifecycle_of,
    weighted_score,
)


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _week_key(dt: datetime) -> str:
    iso = dt.isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"


def _build_sessions(conn: sqlite3.Connection, gap_hours: float) -> dict[str, int | None]:
    """message_id → 会话内 session 序号;无时间戳消息 → None。"""
    gap = timedelta(hours=gap_hours)
    sessions: dict[str, int | None] = {}
    for conv_id, in conn.execute("SELECT id FROM conversation").fetchall():
        rows = conn.execute(
            "SELECT id, ts FROM message WHERE conversation_id = ? ORDER BY seq",
            (conv_id,),
        ).fetchall()
        session_no = 0
        prev_ts: datetime | None = None
        for row in rows:
            ts = _parse_ts(row["ts"])
            if ts is None:
                sessions[row["id"]] = None
                continue
            if prev_ts is not None and ts - prev_ts > gap:
                session_no += 1
            sessions[row["id"]] = session_no
            prev_ts = ts
    return sessions


def _topic_rows(conn: sqlite3.Connection, topic_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT ch.id AS chunk_id, ch.token_count, m.id AS message_id, m.role, m.content,"
        " m.ts, m.seq AS message_seq, m.conversation_id"
        " FROM topic_chunk tc"
        " JOIN chunk ch ON ch.id = tc.chunk_id"
        " JOIN message m ON m.id = ch.message_id"
        " WHERE tc.topic_id = ?"
        " ORDER BY m.conversation_id, m.seq, ch.seq",
        (topic_id,),
    ).fetchall()


def _collect_signals(
    conn: sqlite3.Connection,
    topic_id: str,
    rows: list[sqlite3.Row],
    sessions: dict[str, int | None],
    now: datetime,
) -> TopicSignals:
    stamps = [ts for row in rows if (ts := _parse_ts(row["ts"])) is not None]
    temporal_missing = not stamps
    weekly: dict[str, int] = {}
    for ts in stamps:
        weekly[_week_key(ts)] = weekly.get(_week_key(ts), 0) + 1

    # 会话归属: 有时间戳用 session 序号;否则退化为"会话"粒度
    session_keys: dict[object, int] = {}
    for row in rows:
        if row["ts"] is None:
            continue
        session_keys.setdefault((row["conversation_id"], sessions.get(row["message_id"])), 1)
    touch_counts: dict[object, int] = {}
    for row in rows:
        if row["ts"] is None:
            continue
        key = (row["conversation_id"], sessions.get(row["message_id"]))
        touch_counts[key] = touch_counts.get(key, 0) + 1
    if temporal_missing:  # 无 ts: 以会话为"会话"的近似
        by_conv: dict[str, int] = {}
        for row in rows:
            by_conv[row["conversation_id"]] = by_conv.get(row["conversation_id"], 0) + 1
        distinct_sessions = len(by_conv)
        mean_touch = (
            sum(by_conv.values()) / len(by_conv) if by_conv else 0.0
        )
    else:
        distinct_sessions = len(touch_counts)
        mean_touch = (
            sum(touch_counts.values()) / len(touch_counts) if touch_counts else 0.0
        )

    token_values = [row["token_count"] or 0 for row in rows]
    user_msgs = sum(1 for row in rows if row["role"] == "user")
    followup = sum(
        1 for row in rows
        if row["role"] == "user" and FOLLOWUP_PATTERN.search(row["content"])
    )
    obligation = sum(
        1 for row in rows
        if row["role"] == "user" and OBLIGATION_PATTERN.search(row["content"])
    )
    first_seen = min(stamps) if stamps else None
    last_seen = max(stamps) if stamps else None
    span_weeks = (
        max(1, ceil((last_seen - first_seen).days / 7)) if first_seen and last_seen else 1
    )
    co = conn.execute(
        "SELECT COUNT(DISTINCT o.topic_id) FROM topic_chunk o"
        " WHERE o.topic_id != ? AND o.chunk_id IN ("
        "  SELECT c2.id FROM chunk c2 WHERE c2.conversation_id IN ("
        "   SELECT c.conversation_id FROM topic_chunk tc"
        "   JOIN chunk c ON c.id = tc.chunk_id WHERE tc.topic_id = ?))",
        (topic_id, topic_id),
    ).fetchone()[0]
    projects = conn.execute(
        "SELECT COUNT(*) FROM project WHERE status = 'active' AND topic_ids LIKE ?",
        (f'%{topic_id}%',),
    ).fetchone()[0]

    return TopicSignals(
        tokens_total=sum(token_values),
        active_weeks=len(weekly),
        span_weeks=span_weeks,
        distinct_sessions=distinct_sessions,
        mean_session_touch_msgs=mean_touch,
        mean_chunk_tokens=(
            sum(token_values) / len(token_values) if token_values else 0.0
        ),
        user_msgs=user_msgs,
        followup_hits=followup,
        obligation_hits=obligation,
        co_occurring_topics=int(co),
        linked_active_projects=int(projects),
        days_since_last=(
            (now - last_seen).total_seconds() / 86400.0 if last_seen else None
        ),
        weekly_counts=weekly,
        temporal_missing=temporal_missing,
    )


def _component_evidence(
    conn: sqlite3.Connection,
    topic_id: str,
    component: str,
    rows: list[sqlite3.Row],
    run_id: str,
    now: str,
) -> None:
    """每分量写 top-3 代表证据;followup/obligation 用命中消息。"""
    if component in ("followup_density", "obligation"):
        pattern = FOLLOWUP_PATTERN if component == "followup_density" else OBLIGATION_PATTERN
        picked = [
            row for row in rows
            if row["role"] == "user" and pattern.search(row["content"])
        ][:3]
    else:
        picked = sorted(
            rows, key=lambda r: (-(r["token_count"] or 0), r["chunk_id"])
        )[:3]
    for row in picked:
        conn.execute(
            "INSERT OR IGNORE INTO evidence (id, subject_type, subject_id, message_id,"
            " chunk_id, quote, signal_type, weight, run_id, created_at)"
            " VALUES (?, 'interest', ?, ?, ?, ?, ?, 1.0, ?, ?)",
            (stable_id("evidence", "interest", topic_id, row["chunk_id"], component),
             topic_id, row["message_id"], row["chunk_id"],
             row["content"][:200], f"signal:{component}", run_id, now),
        )


def _upsert_interest(
    conn: sqlite3.Connection,
    topic_id: str,
    score: float,
    scores: dict[str, float | None],
    raw_stats: dict[str, object],
    interest_type: str,
    lifecycle: str | None,
    confidence: float,
    run_id: str,
    now: str,
    first_seen: str | None,
    last_seen: str | None,
) -> str:
    """写入当前值;实质变化(Δ≥0.05 / lifecycle / type)才写 revision。返回动作名。"""
    old = conn.execute(
        "SELECT score, lifecycle, interest_type FROM interest WHERE topic_id = ?",
        (topic_id,),
    ).fetchone()
    action = "new"
    if old is None:
        conn.execute(
            "INSERT INTO interest (topic_id, score, scores, raw_stats, interest_type,"
            " lifecycle, confidence, inference_method, model_version, first_seen,"
            " last_seen, computed_at, run_id, updated_at)"
            " VALUES (?,?,?,?,?,?,?,'signal_rules','v0.1',?,?,?,?,?)",
            (topic_id, score, json.dumps(scores), json.dumps(raw_stats, ensure_ascii=False),
             interest_type, lifecycle, confidence, first_seen, last_seen, now, run_id, now),
        )
        return action

    changes: list[str] = []
    if old["score"] is not None and abs(score - old["score"]) >= 0.05:
        changes.append(f"score:{old['score']:.3f}->{score:.3f}")
    if old["lifecycle"] != lifecycle:
        changes.append(f"lifecycle:{old['lifecycle']}->{lifecycle}")
    if old["interest_type"] != interest_type:
        changes.append(f"type:{old['interest_type']}->{interest_type}")
    if changes:
        seq = conn.execute(
            "SELECT COUNT(*) FROM entity_revision WHERE entity_id = ?", (topic_id,)
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO entity_revision (id, entity_type, entity_id, previous_state,"
            " new_state, reason, run_id, created_at)"
            " VALUES (?, 'interest', ?, ?, ?, ?, ?, ?)",
            (stable_id("revision", topic_id, now, "interest", str(seq)),
             topic_id,
             json.dumps({"score": old["score"], "lifecycle": old["lifecycle"],
                         "interest_type": old["interest_type"]}),
             json.dumps({"score": round(score, 4), "lifecycle": lifecycle,
                         "interest_type": interest_type}),
             "recompute:" + ";".join(changes), run_id, now),
        )
        action = "revised"
    else:
        action = "drifted"
    conn.execute(
        "UPDATE interest SET score = ?, scores = ?, raw_stats = ?, interest_type = ?,"
        " lifecycle = ?, confidence = ?, first_seen = COALESCE(?, first_seen),"
        " last_seen = COALESCE(?, last_seen), computed_at = ?, run_id = ?, updated_at = ?"
        " WHERE topic_id = ?",
        (score, json.dumps(scores), json.dumps(raw_stats, ensure_ascii=False),
         interest_type, lifecycle, confidence, first_seen, last_seen, now, run_id, now,
         topic_id),
    )
    return action


def run_interests_stage(
    conn: sqlite3.Connection,
    config: PsiosConfig,
    run_id: str,
    now: str,
) -> dict[str, object]:
    """对所有有 chunk 的 active topic 计算兴趣评分并落库。"""
    now_dt = _parse_ts(now)
    assert now_dt is not None
    sessions = _build_sessions(conn, config.lifecycle.session_gap_hours)

    topics = conn.execute(
        "SELECT t.id, t.label, t.first_seen, t.last_seen FROM topic t"
        " WHERE t.status = 'active' AND EXISTS ("
        "  SELECT 1 FROM topic_chunk tc WHERE tc.topic_id = t.id)"
        " ORDER BY t.id"
    ).fetchall()

    token_max_rows = conn.execute(
        "SELECT tc.topic_id AS t, SUM(ch.token_count) AS tokens FROM topic_chunk tc"
        " JOIN chunk ch ON ch.id = tc.chunk_id GROUP BY tc.topic_id"
    ).fetchall()
    max_tokens = max((row["tokens"] or 0) for row in token_max_rows) if token_max_rows else 0

    signals_by_topic: dict[str, TopicSignals] = {}
    rows_by_topic: dict[str, list[sqlite3.Row]] = {}
    for topic in topics:
        rows = _topic_rows(conn, topic["id"])
        rows_by_topic[topic["id"]] = rows
        signals_by_topic[topic["id"]] = _collect_signals(
            conn, topic["id"], rows, sessions, now_dt
        )
    max_co = max(
        (s.co_occurring_topics for s in signals_by_topic.values()), default=0
    )

    actions = {"new": 0, "revised": 0, "drifted": 0}
    result_rows: list[dict[str, object]] = []
    for topic in topics:
        topic_id = topic["id"]
        signals = signals_by_topic[topic_id]
        components = compute_components(
            signals, int(max_tokens), max_co,
            config.lifecycle.recency_half_life_days,
        )
        score = weighted_score(components, config.interest_weights or {})
        lifecycle = lifecycle_of(signals, config.lifecycle, now_dt)
        interest_type = interest_type_of(lifecycle, components)
        evidence_count = min(3, len(rows_by_topic[topic_id])) * 10
        confidence = confidence_of(
            evidence_count, config.confidence.evidence_saturation,
            signals.temporal_missing,
        )
        scores_dict: dict[str, float | None] = {
            "frequency": round(components.frequency, 4),
            "recency": None if components.recency is None else round(components.recency, 4),
            "persistence": (
                None if components.persistence is None
                else round(components.persistence, 4)
            ),
            "depth": round(components.depth, 4),
            "return_rate": (
                None if components.return_rate is None
                else round(components.return_rate, 4)
            ),
            "followup_density": round(components.followup_density, 4),
            "project_conversion": round(components.project_conversion, 4),
            "connectivity": round(components.connectivity, 4),
            "voluntary": round(components.voluntary, 4),
            "obligation": round(components.obligation, 4),
        }
        raw_stats: dict[str, object] = {
            "tokens_total": signals.tokens_total,
            "active_weeks": signals.active_weeks,
            "span_weeks": signals.span_weeks,
            "distinct_sessions": signals.distinct_sessions,
            "user_msgs": signals.user_msgs,
            "followup_hits": signals.followup_hits,
            "obligation_hits": signals.obligation_hits,
            "co_occurring_topics": signals.co_occurring_topics,
            "days_since_last": (
                None if signals.days_since_last is None
                else round(signals.days_since_last, 2)
            ),
            "temporal_missing": signals.temporal_missing,
        }
        action = _upsert_interest(
            conn, topic_id, round(score, 4), scores_dict, raw_stats,
            interest_type, lifecycle, confidence, run_id, now,
            topic["first_seen"], topic["last_seen"],
        )
        actions[action] += 1
        for component in scores_dict:
            _component_evidence(
                conn, topic_id, component, rows_by_topic[topic_id], run_id, now
            )
        result_rows.append(
            {"topic": topic_id, "label": topic["label"], "score": round(score, 4),
             "lifecycle": lifecycle, "type": interest_type, "confidence": confidence}
        )

    result_rows.sort(
        key=lambda r: (-float(str(r["score"])), str(r["topic"]))
    )
    return {"actions": actions, "interests": result_rows}
