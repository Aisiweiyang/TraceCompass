"""Invariant 检查器(data-model.md §11)。

可静态检查的不变量在此实现;行为类不变量(重复导入幂等、UPDATE 前必有
revision)由 M1+ 的 DAO 与对应测试保证,不在静态检查范围 —— 见各函数 docstring。

用法: ``run_all(conn)`` 返回 {检查名: [违例描述]},空 dict 表示全部通过;
``assert_no_violations(conn)`` 在有违例时抛 InvariantViolation。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import datetime

# 含 *_at / *_seen / ts 时间戳的 (表, 列);*_seen/*_at 允许 NULL
_TIMESTAMP_COLUMNS: list[tuple[str, str]] = [
    *(("conversation", col) for col in ("started_at", "ended_at", "created_at")),
    *(("message", col) for col in ("ts", "created_at")),
    ("chunk", "created_at"),
    *(("topic", col) for col in ("first_seen", "last_seen", "created_at", "updated_at")),
    ("interest", "computed_at"),
    *(("meta_interest", col) for col in ("created_at", "updated_at")),
    *(("goal", col) for col in ("first_seen", "last_seen", "created_at", "updated_at")),
    *(("project", col) for col in ("first_seen", "last_seen", "created_at", "updated_at")),
    *(("question", col) for col in ("first_seen", "last_seen", "created_at", "updated_at")),
    ("question_occurrence", "ts"),
    ("evidence", "created_at"),
    ("entity_revision", "created_at"),
    ("personal_model_snapshot", "generated_at"),
    ("human_action", "created_at"),
]

# 需要证据覆盖的推断实体: (subject_type, 表, 主键列)——interest 以 topic_id 为主键
_INFERRED_SUBJECTS: list[tuple[str, str, str]] = [
    ("interest", "interest", "topic_id"),
    ("goal", "goal", "id"),
    ("project", "project", "id"),
    ("question", "question", "id"),
    ("meta_interest", "meta_interest", "id"),
]


class InvariantViolation(AssertionError):
    """存在不变量违例。"""


Check = Callable[[sqlite3.Connection], list[str]]


def check_confidence_range(conn: sqlite3.Connection) -> list[str]:
    """不变量 #5: 所有推断实体的 confidence ∈ [0,1] 或为 NULL。"""
    violations: list[str] = []
    for table in ("topic", "interest", "meta_interest", "goal", "project", "question"):
        row = conn.execute(
            f"SELECT COUNT(*) FROM {table} "  # noqa: S608 — 表名来自内部常量
            "WHERE confidence IS NOT NULL AND (confidence < 0 OR confidence > 1)"
        ).fetchone()
        if row[0]:
            violations.append(f"{table}: {row[0]} 行 confidence 越界 [0,1]")
    return violations


def check_evidence_coverage(conn: sqlite3.Connection) -> list[str]:
    """不变量 #3: 每个推断实体至少 1 条 evidence。"""
    violations: list[str] = []
    for subject_type, table, key_col in _INFERRED_SUBJECTS:
        row = conn.execute(
            f"SELECT COUNT(*) FROM {table} t WHERE NOT EXISTS "  # noqa: S608
            f"(SELECT 1 FROM evidence e WHERE e.subject_type = ? AND e.subject_id = t.{key_col})",
            (subject_type,),
        ).fetchone()
        if row[0]:
            violations.append(f"{table}: {row[0]} 行没有任何 evidence(推断必须有证据)")
    return violations


def check_merged_topics(conn: sqlite3.Connection) -> list[str]:
    """不变量 #4: merged topic 不得删除,且必须指向存在的目标。"""
    violations: list[str] = []
    null_target = conn.execute(
        "SELECT COUNT(*) FROM topic WHERE status = 'merged' AND merged_into_id IS NULL"
    ).fetchone()[0]
    if null_target:
        violations.append(f"topic: {null_target} 行 status='merged' 但 merged_into_id 为空")
    dangling = conn.execute(
        "SELECT COUNT(*) FROM topic t WHERE t.status = 'merged' AND t.merged_into_id IS NOT NULL "
        "AND NOT EXISTS (SELECT 1 FROM topic target WHERE target.id = t.merged_into_id)"
    ).fetchone()[0]
    if dangling:
        violations.append(f"topic: {dangling} 行 merged_into_id 指向不存在的 topic")
    return violations


def _parse_ts(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def check_iso_timestamps(conn: sqlite3.Connection) -> list[str]:
    """不变量 #5(后半): 所有非 NULL 时间戳是合法 ISO8601。"""
    violations: list[str] = []
    for table, column in _TIMESTAMP_COLUMNS:
        rows = conn.execute(
            f"SELECT {column} FROM {table} WHERE {column} IS NOT NULL"  # noqa: S608
        ).fetchall()
        for (value,) in rows:
            if not isinstance(value, str):
                violations.append(f"{table}.{column}: 非文本时间戳 {value!r}")
                continue
            try:
                _parse_ts(value)
            except ValueError:
                violations.append(f"{table}.{column}: 非法 ISO8601 {value!r}")
    return violations


ALL_CHECKS: list[tuple[str, Check]] = [
    ("confidence_range", check_confidence_range),
    ("evidence_coverage", check_evidence_coverage),
    ("merged_topics", check_merged_topics),
    ("iso_timestamps", check_iso_timestamps),
]


def run_all(conn: sqlite3.Connection) -> dict[str, list[str]]:
    return {name: check(conn) for name, check in ALL_CHECKS}


def assert_no_violations(conn: sqlite3.Connection) -> None:
    """全部通过则静默;否则抛出携带全部违例信息的 InvariantViolation。"""
    results = run_all(conn)
    violations = [(name, msgs) for name, msgs in results.items() if msgs]
    if violations:
        lines = [f"{name}: {msg}" for name, msgs in violations for msg in msgs]
        raise InvariantViolation("违反数据库不变量:\n" + "\n".join(lines))
