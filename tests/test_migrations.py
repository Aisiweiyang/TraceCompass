"""迁移 runner:幂等、版本推进、非法文件拒绝。"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from psios.store import MigrationError, connect, current_version, migrate

EXPECTED_TABLES = {
    "model_run", "source", "conversation", "message", "chunk", "topic", "topic_chunk",
    "interest", "meta_interest", "goal", "project", "question", "question_occurrence",
    "evidence", "entity_revision", "personal_model_snapshot", "human_action",
}
EXPECTED_VIEWS = {"v_topic_weekly", "v_interest_current"}
LATEST_VERSION = 2


def _objects(conn: sqlite3.Connection, kind: str) -> set[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = ? AND name NOT LIKE 'sqlite_%'", (kind,)
    ).fetchall()
    return {row[0] for row in rows}


def test_migrate_creates_full_schema(db) -> None:
    assert current_version(db) == LATEST_VERSION
    assert EXPECTED_TABLES <= _objects(db, "table")
    assert EXPECTED_VIEWS <= _objects(db, "view")


def test_migrate_is_idempotent(db) -> None:
    assert migrate(db) == []          # 再次运行: 无新迁移
    assert current_version(db) == LATEST_VERSION
    assert EXPECTED_TABLES <= _objects(db, "table")


def test_views_are_queryable(db) -> None:
    # 空库上两个视图必须可查询(列名与 data-model.md §10 一致)
    cols = {row[1] for row in db.execute("PRAGMA table_info(v_topic_weekly)")}
    assert {"topic_id", "week", "chunk_count", "tokens"} <= cols
    cols = {row[1] for row in db.execute("PRAGMA table_info(v_interest_current)")}
    assert {"topic_id", "score", "scores", "raw_stats", "lifecycle", "label"} <= cols


def test_fk_enforcement(db) -> None:
    """conversation.source_id 引用不存在的 source → 立即 IntegrityError。"""
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT INTO conversation (id, source_id, external_id, content_hash, created_at) "
            "VALUES ('c1', 'no-such-source', 'e1', 'h', '2026-09-08T00:00:00')"
        )


def test_unique_constraints_back_dedupe(db) -> None:
    """不变量 #1 的 schema 基础: 去重唯一约束存在。"""
    db.execute(
        "INSERT INTO source VALUES ('s1','n','jsonl','2026-09-08T00:00:00',NULL,NULL)"
    )
    db.execute(
        "INSERT INTO conversation VALUES ('c1','s1','ext1',NULL,NULL,NULL,NULL,'h1',NULL,'now')"
    )
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT INTO conversation VALUES ('c2','s1','ext1',NULL,NULL,NULL,NULL,'h1',NULL,'now')"
        )


def test_bad_migration_filename_rejected(db, tmp_path: Path) -> None:
    (tmp_path / "init.sql").write_text("CREATE TABLE t(x);", encoding="utf-8")
    with pytest.raises(MigrationError):
        migrate(db, tmp_path)


def test_duplicate_version_rejected(db, tmp_path: Path) -> None:
    (tmp_path / "002_a.sql").write_text("CREATE TABLE a(x);", encoding="utf-8")
    (tmp_path / "002_b.sql").write_text("CREATE TABLE b(x);", encoding="utf-8")
    with pytest.raises(MigrationError):
        migrate(db, tmp_path)


def test_failed_migration_rolls_back_completely(db, tmp_path: Path) -> None:
    bad = tmp_path / "003_bad.sql"
    bad.write_text(
        "CREATE TABLE ok_table(x TEXT);\n"
        "INSERT INTO topic VALUES (NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL,"
        " NULL, NULL, NULL, NULL, NULL, NULL, NULL);",  # 非法 → 整个迁移回滚
        encoding="utf-8",
    )
    with pytest.raises(MigrationError):
        migrate(db, tmp_path)
    exists = db.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE name = 'ok_table'"
    ).fetchone()[0]
    assert exists == 0, "失败迁移必须整体回滚, 不得留下半套表"


def test_connect_wal_and_foreign_keys(tmp_path: Path) -> None:
    conn = connect(tmp_path / "fresh.db")
    try:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    finally:
        conn.close()
