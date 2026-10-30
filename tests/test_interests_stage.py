"""interests stage 集成:受控日期 fixture → 生命周期/评分/revision/证据。"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from psios.config import PsiosConfig, load_config
from psios.ids import stable_id
from psios.inference.interests import run_interests_stage
from psios.pipeline import runs as runs_mod
from psios.store import invariants

NOW_DT = datetime(2026, 9, 13, 12, 0, tzinfo=UTC)
NOW = NOW_DT.isoformat()


@pytest.fixture()
def stage_config(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(
        '[storage]\ndb_path = "x.db"\n\n'
        "[interest_weights]\n"
        "frequency = 0.15\nrecency = 0.10\npersistence = 0.20\ndepth = 0.10\n"
        "return_rate = 0.15\nfollowup_density = 0.05\nproject_conversion = 0.10\n"
        "connectivity = 0.05\nvoluntary = 0.10\n",
        encoding="utf-8",
    )
    return load_config(path)


def _insert_topic(db, topic_id: str, label: str) -> None:
    db.execute(
        "INSERT INTO topic (id, label, label_source, inference_method, status,"
        " created_at, updated_at) VALUES (?,?,'keyword','leader_cluster','active',?,?)",
        (topic_id, label, NOW, NOW),
    )


def _insert_message(
    db,
    message_id: str,
    conversation_id: str,
    seq: int,
    content: str,
    days_ago: int,
    hour: int = 10,
) -> str:
    """插入 user 消息(含缺失的 conversation 父行)并返回关联 chunk 的 id。"""
    ts = (NOW_DT - timedelta(days=days_ago)).replace(hour=hour).isoformat()
    created = (NOW_DT - timedelta(days=days_ago + 1)).isoformat()
    db.execute(
        "INSERT OR IGNORE INTO conversation (id, source_id, external_id, content_hash,"
        " created_at) VALUES (?,'s1',?, 'h', ?)",
        (conversation_id, f"ext-{conversation_id}", created),
    )
    db.execute(
        "INSERT INTO message (id, conversation_id, seq, role, content, ts, content_hash,"
        " meta, created_at) VALUES (?,?,?,'user',?,?,'h','{}',?)",
        (message_id, conversation_id, seq, content, ts, created),
    )
    chunk_id = stable_id("test-chunk", message_id)
    db.execute(
        "INSERT INTO chunk (id, conversation_id, message_id, seq, text, token_count,"
        " created_at) VALUES (?,?,?,0,?,?,?)",
        (chunk_id, conversation_id, message_id, content, len(content), NOW),
    )
    return chunk_id


def _link(db, topic_id: str, chunk_id: str) -> None:
    db.execute(
        "INSERT OR IGNORE INTO topic_chunk (topic_id, chunk_id, weight, method)"
        " VALUES (?,? ,1.0,'leader')",
        (topic_id, chunk_id),
    )


@pytest.fixture()
def seeded(db):
    """tax(单周爆量,40 天前)+ stable(半年持续,每周同小时多消息)。"""
    db.execute(
        "INSERT INTO model_run (id, config_snapshot, status, started_at)"
        " VALUES ('r0','{}','done','2026-01-01T00:00:00')"
    )
    db.execute("INSERT INTO source VALUES ('s1','n','jsonl','2026-01-01T00:00:00',NULL,NULL)")

    _insert_topic(db, "t-tax", "增值税申报")
    for i in range(12):
        chunk = _insert_message(
            db, f"m-tax-{i}", "c-tax", i, f"报税表格第{i}项怎么填", 40, hour=10 + i
        )
        _link(db, "t-tax", chunk)

    _insert_topic(db, "t-stable", "个人知识管理")
    week = 0
    for days_ago in range(175, -1, -7):
        for j in range(3):
            # 内容 ≥400 token(≈字符数)以满足 depth 分量的深挖条件
            content = (
                f"Obsidian 知识管理实践第{week}轮,整理双向链接笔记 {j}。"
                + "回顾并补充笔记内容,维护标签体系与本地知识库。 " * 26
            )
            chunk = _insert_message(
                db, f"m-pkm-{week}-{j}", f"c-pkm-{week}", j, content, days_ago, hour=9 + j,
            )
            _link(db, "t-stable", chunk)
        week += 1
    db.commit()
    return db


def _run(db, config: PsiosConfig) -> dict:
    run_id = runs_mod.start_run(db, ["interest"], config.snapshot())
    return run_interests_stage(db, config, run_id, NOW)


def _interest(db, topic_id: str):
    return db.execute(
        "SELECT * FROM interest WHERE topic_id = ?", (topic_id,)
    ).fetchone()


def test_tax_case_classified_temporary(seeded, stage_config) -> None:
    """fixture A: 一周爆量报税 → temporary + persistence < 0.3 + 低分。"""
    _run(seeded, stage_config)
    row = _interest(seeded, "t-tax")
    assert row is not None
    assert row["lifecycle"] == "temporary"
    assert row["interest_type"] == "temporary"
    scores = json.loads(row["scores"])
    assert scores["persistence"] is not None and scores["persistence"] < 0.3
    assert row["score"] < 0.5


def test_stable_case_classified_stable_intrinsic(seeded, stage_config) -> None:
    """fixture B: 半年持续深挖、无义务信号 → stable + intrinsic + 高 persistence。"""
    _run(seeded, stage_config)
    row = _interest(seeded, "t-stable")
    assert row is not None
    assert row["lifecycle"] == "stable"
    assert row["interest_type"] == "intrinsic"
    scores = json.loads(row["scores"])
    assert scores["persistence"] >= 0.6
    assert scores["obligation"] == 0.0
    assert row["score"] > _interest(seeded, "t-tax")["score"]


def test_evidence_coverage_per_component(seeded, stage_config) -> None:
    _run(seeded, stage_config)
    signals = seeded.execute(
        "SELECT signal_type, COUNT(*) FROM evidence WHERE subject_id = 't-stable'"
        " GROUP BY signal_type"
    ).fetchall()
    names = {row[0] for row in signals}
    for component in ("frequency", "recency", "persistence", "depth", "voluntary"):
        assert f"signal:{component}" in names
    invariants.assert_no_violations(seeded)


def test_rerun_drifts_without_revision(seeded, stage_config) -> None:
    _run(seeded, stage_config)
    revisions_before = seeded.execute("SELECT COUNT(*) FROM entity_revision").fetchone()[0]
    stats = _run(seeded, stage_config)
    assert stats["actions"]["drifted"] == 2
    assert stats["actions"]["revised"] == 0
    revisions_after = seeded.execute("SELECT COUNT(*) FROM entity_revision").fetchone()[0]
    assert revisions_after == revisions_before  # 无实质变化不写 revision


def test_new_activity_revises_lifecycle(seeded, stage_config) -> None:
    _run(seeded, stage_config)
    assert _interest(seeded, "t-tax")["lifecycle"] == "temporary"

    # 报税主题最近重新活跃(3 天前新消息)
    chunk = _insert_message(seeded, "m-tax-new", "c-tax", 99, "补交季度报税材料", 3, hour=15)
    _link(seeded, "t-tax", chunk)
    seeded.commit()
    stats = _run(seeded, stage_config)
    assert stats["actions"]["revised"] >= 1
    revisions = seeded.execute(
        "SELECT COUNT(*) FROM entity_revision WHERE entity_id = 't-tax'"
    ).fetchone()[0]
    assert revisions >= 1
    new_row = _interest(seeded, "t-tax")
    assert new_row["lifecycle"] != "temporary"  # 最近活跃不再满足 temporary 条件
    invariants.assert_no_violations(seeded)


def test_no_timestamp_topic_degrades_gracefully(db, stage_config) -> None:
    db.execute(
        "INSERT INTO model_run (id, config_snapshot, status, started_at)"
        " VALUES ('r0','{}','done','2026-01-01T00:00:00')"
    )
    db.execute("INSERT INTO source VALUES ('s1','n','jsonl','2026-01-01T00:00:00',NULL,NULL)")
    db.execute(
        "INSERT INTO conversation (id, source_id, external_id, content_hash, created_at)"
        " VALUES ('c-n','s1','ext-cn','h','2026-01-01T00:00:00')"
    )
    _insert_topic(db, "t-notes", "无时间戳笔记")
    for i in range(10):
        db.execute(
            "INSERT INTO message (id, conversation_id, seq, role, content, ts, content_hash,"
            " meta, created_at) VALUES (?,?,?,'user',?,NULL,'h','{}',?)",
            (f"m-n-{i}", "c-n", i, f"笔记内容{i}", NOW),
        )
        chunk = stable_id("test-chunk", f"m-n-{i}")
        db.execute(
            "INSERT INTO chunk (id, conversation_id, message_id, seq, text, token_count,"
            " created_at) VALUES (?,?,?,0,?,?,?)",
            (chunk, "c-n", f"m-n-{i}", f"笔记内容{i}", 10, NOW),
        )
        _link(db, "t-notes", chunk)
    db.commit()

    stats = _run(db, stage_config)
    assert stats["actions"]["new"] == 1
    row = _interest(db, "t-notes")
    assert row is not None
    assert row["lifecycle"] is None  # 无时间戳 → 不做生命周期分类
    scores = json.loads(row["scores"])
    assert scores["recency"] is None and scores["persistence"] is None
    raw = json.loads(row["raw_stats"])
    assert raw["temporal_missing"] is True
    invariants.assert_no_violations(db)
