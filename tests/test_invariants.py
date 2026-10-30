"""Invariant 检查器:空库通过,构造的违例必须被检出。"""

from __future__ import annotations

import pytest

from psios.store import invariants

# 各表插入所需的最小合法行
_RUN = "INSERT INTO model_run (id, config_snapshot, status, started_at) "
_RUN += "VALUES ('r1', '{}', 'running', '2026-09-08T00:00:00')"
_SOURCE = "INSERT INTO source VALUES ('s1','n','jsonl','2026-09-08T00:00:00',NULL,NULL)"
_CONV = (
    "INSERT INTO conversation VALUES ('c1','s1','e1',NULL,NULL,NULL,NULL,'h1',NULL,"
    "'2026-09-08T00:00:00')"
)
_MSG = (
    "INSERT INTO message VALUES ('m1','c1',1,'user','内容','2026-09-08T00:00:00','h2',NULL,"
    "'2026-09-08T00:00:00')"
)
_TOPIC = (
    "INSERT INTO topic (id, label, label_source, inference_method, status, created_at,"
    " updated_at) VALUES ('t1','L','keyword','m','active','2026-09-08T00:00:00',"
    "'2026-09-08T00:00:00')"
)


def _insert_topic_with_interest(conn) -> None:
    conn.execute(_RUN)
    conn.execute(_SOURCE)
    conn.execute(_CONV)
    conn.execute(_MSG)
    conn.execute(_TOPIC)
    conn.execute(
        "INSERT INTO interest (topic_id, inference_method, computed_at, updated_at) "
        "VALUES ('t1','m','2026-09-08T00:00:00','2026-09-08T00:00:00')"
    )


def _add_evidence(conn, subject_type: str, subject_id: str) -> None:
    conn.execute(
        "INSERT INTO evidence (id, subject_type, subject_id, message_id, quote, created_at) "
        "VALUES (?,?,?,?,?,?)",
        (f"ev-{subject_id}", subject_type, subject_id, "m1", "引用", "2026-09-08T00:00:00"),
    )


def test_empty_database_passes_all(db) -> None:
    results = invariants.run_all(db)
    assert all(msgs == [] for msgs in results.values()), results
    invariants.assert_no_violations(db)


def test_confidence_out_of_range_detected(db) -> None:
    _insert_topic_with_interest(db)
    _add_evidence(db, "interest", "t1")
    db.execute("UPDATE interest SET confidence = 1.5")
    with pytest.raises(invariants.InvariantViolation, match="越界"):
        invariants.assert_no_violations(db)


def test_missing_evidence_detected(db) -> None:
    _insert_topic_with_interest(db)  # interest 无 evidence
    with pytest.raises(invariants.InvariantViolation, match="没有任何 evidence"):
        invariants.assert_no_violations(db)


def test_evidence_coverage_satisfied(db) -> None:
    _insert_topic_with_interest(db)
    _add_evidence(db, "interest", "t1")
    invariants.assert_no_violations(db)  # 不抛即通过


def test_merged_topic_without_target_detected(db) -> None:
    conn = db
    conn.execute(_RUN)
    conn.execute(_SOURCE)
    conn.execute(_CONV)
    conn.execute(_MSG)
    conn.execute(_TOPIC)
    _add_evidence(conn, "topic", "t1")
    conn.execute("UPDATE topic SET status = 'merged'")  # merged_into_id 为 NULL
    with pytest.raises(invariants.InvariantViolation, match="merged_into_id 为空"):
        invariants.assert_no_violations(db)


def test_merged_topic_dangling_target_detected(db) -> None:
    conn = db
    conn.execute(_RUN)
    conn.execute(_SOURCE)
    conn.execute(_CONV)
    conn.execute(_MSG)
    conn.execute(_TOPIC)
    _add_evidence(conn, "topic", "t1")
    # 外键开启时悬空目标根本写不进去;此检查防御的是约束外的损坏(导入工具/人为改动)
    # PRAGMA foreign_keys 在未提交事务内是 no-op,先提交
    conn.commit()
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.execute("UPDATE topic SET status = 'merged', merged_into_id = 'ghost'")
    conn.commit()
    conn.execute("PRAGMA foreign_keys=ON")
    with pytest.raises(invariants.InvariantViolation, match="不存在"):
        invariants.assert_no_violations(db)


def test_bad_timestamp_detected(db) -> None:
    conn = db
    conn.execute(_RUN)
    conn.execute(_SOURCE)
    conn.execute(_CONV)
    conn.execute(_MSG)
    conn.execute(_TOPIC)
    _add_evidence(conn, "topic", "t1")
    conn.execute("UPDATE message SET ts = 'yesterday'")
    with pytest.raises(invariants.InvariantViolation, match="ISO8601"):
        invariants.assert_no_violations(db)


def test_z_suffix_timestamp_accepted(db) -> None:
    conn = db
    conn.execute(_RUN)
    conn.execute(_SOURCE)
    conn.execute(_CONV)
    conn.execute(_MSG)
    conn.execute(_TOPIC)
    _add_evidence(conn, "topic", "t1")
    conn.execute("UPDATE message SET ts = '2026-09-08T00:00:00Z'")
    invariants.assert_no_violations(db)
