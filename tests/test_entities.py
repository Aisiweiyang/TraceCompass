"""entities stage:goal/project/question 规则检测、recurring、幂等。"""

from __future__ import annotations

import json

import pytest

from psios.inference.entities import run_entities_stage
from psios.ingestion import import_file
from psios.ingestion.jsonl import JsonlAdapter
from psios.store import invariants

NOW = "2026-09-13T12:00:00+00:00"


def _import_texts(db, tmp_path, name: str, texts: list[dict]) -> None:
    path = tmp_path / name
    path.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in texts),
        encoding="utf-8",
    )
    import_file(db, path, JsonlAdapter(), NOW)


def _run_stage(db, run_id: str) -> dict:
    """stage 前先落 model_run 行(run_id 外键要求)。"""
    db.execute(
        "INSERT OR IGNORE INTO model_run (id, config_snapshot, status, started_at)"
        " VALUES (?,'{}','running','2026-09-13T12:00:00')",
        (run_id,),
    )
    return run_entities_stage(db, run_id, NOW)


@pytest.fixture()
def entity_db(db, tmp_path):
    _import_texts(db, tmp_path, "goals.jsonl", [
        {
            "role": "user", "content": "我的目标是在年底前上线 PSI OS V0.1",
            "ts": "2026-01-10T10:00:00",
        },
        {
            "role": "user", "content": "我打算三个月内学会统计学习理论。",
            "ts": "2026-01-15T10:00:00",
        },
        {"role": "user", "content": "今天天气不错,出去走走。", "ts": "2026-01-16T10:00:00"},
    ])
    _import_texts(db, tmp_path, "projects.jsonl", [
        {
            "role": "user", "content": "我在做个人战略智能系统,进展顺利。",
            "ts": "2026-02-01T09:00:00",
        },
        {
            "role": "user", "content": "I'm working on psi-os dashboard now.",
            "ts": "2026-02-03T09:00:00",
        },
        {"role": "user", "content": "这个项目的事情先放一放。", "ts": "2026-02-04T09:00:00"},
    ])
    questions = []
    for week, day in ((0, "2026-03-02"), (1, "2026-03-09"), (2, "2026-03-16")):
        questions.append({"role": "user",
                          "content": f"embedding 模型选哪个适合中英混合?第{week}次问",
                          "ts": f"{day}T10:00:00"})
    questions.append({"role": "user", "content": "晚饭吃什么?", "ts": "2026-03-17T18:00:00"})
    _import_texts(db, tmp_path, "questions.jsonl", questions)
    return db


def test_goal_detection_explicit_and_inferred(entity_db) -> None:
    stats = _run_stage(entity_db, "run-1")
    assert stats["goals_inserted"] == 2
    goals = entity_db.execute(
        "SELECT statement, kind, epistemic_status FROM goal ORDER BY statement"
    ).fetchall()
    kinds = {row["kind"] for row in goals}
    assert kinds == {"explicit", "inferred"}
    for row in goals:
        expected = "USER_STATED" if row["kind"] == "explicit" else "INFERRED"
        assert row["epistemic_status"] == expected
    # 非目标句不产生 goal
    assert all("天气" not in row["statement"] for row in goals)
    invariants.assert_no_violations(entity_db)


def test_project_detection_filters_stopwords(entity_db) -> None:
    _run_stage(entity_db, "run-1")
    names = [row[0] for row in entity_db.execute("SELECT name FROM project").fetchall()]
    assert any("个人战略智能系统" in name for name in names)
    assert any("psi-os" in name.lower() for name in names)
    assert not any(name in ("这个", "事情", "项目") for name in names)


def test_question_recurring_threshold(entity_db) -> None:
    _run_stage(entity_db, "run-1")
    rows = entity_db.execute(
        "SELECT normalized_text, display_text, count, distinct_weeks, status"
        " FROM question"
    ).fetchall()
    recurring = [row for row in rows if row["status"] == "recurring"]
    assert len(recurring) == 1
    assert recurring[0]["count"] == 3
    assert recurring[0]["distinct_weeks"] == 3
    # 同一问题的每次出现都有 occurrence 记录
    occ = entity_db.execute(
        "SELECT COUNT(*) FROM question_occurrence qo JOIN question q ON q.id = qo.question_id"
        " WHERE q.status = 'recurring'"
    ).fetchone()[0]
    assert occ == 3
    # 晚饭问题: 1 次 → open
    open_rows = [row for row in rows if row["status"] == "open"]
    assert len(open_rows) == 1
    assert "晚饭" in open_rows[0]["display_text"]


def test_rerun_is_idempotent(entity_db) -> None:
    first = _run_stage(entity_db, "run-1")
    totals_first = {
        entity_db.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        for t in ("goal", "project", "question", "question_occurrence", "evidence")
    }
    second = _run_stage(entity_db, "run-2")
    assert second["goals_inserted"] == 0
    assert second["projects_inserted"] == 0
    assert second["questions_inserted"] == 0
    totals_second = {
        entity_db.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        for t in ("goal", "project", "question", "question_occurrence", "evidence")
    }
    assert totals_first == totals_second  # 行数不变
    assert first["goals_inserted"] > 0


def test_question_status_change_writes_revision(db, tmp_path) -> None:
    """逐字重复的问题跨 3 周出现 → open → recurring,写 revision。

    (规则路径的重复 = 近逐字;改写型合并属 embedding 路径,research-log 已记录)
    """
    texts = [
        {"role": "user", "content": "如何评估聚类质量?", "ts": "2026-01-05T10:00:00"},
        {"role": "user", "content": "如何评估聚类质量?", "ts": "2026-01-12T10:00:00"},
    ]
    _import_texts(db, tmp_path, "q1.jsonl", texts)
    _run_stage(db, "run-1")
    row = db.execute("SELECT status FROM question").fetchone()
    assert row["status"] == "open"  # 2 次 → open

    _import_texts(db, tmp_path, "q2.jsonl", [
        {"role": "user", "content": "如何评估聚类质量?", "ts": "2026-01-20T10:00:00"},
    ])
    stats = _run_stage(db, "run-2")
    assert stats["questions_turned_recurring"] == 1
    assert db.execute("SELECT status FROM question").fetchone()["status"] == "recurring"
    revisions = db.execute(
        "SELECT COUNT(*) FROM entity_revision WHERE entity_type = 'question'"
    ).fetchone()[0]
    assert revisions == 1


def test_empty_db_no_crash(db) -> None:
    stats = _run_stage(db, "run-empty")
    assert stats["goals_inserted"] == 0
    invariants.assert_no_violations(db)
