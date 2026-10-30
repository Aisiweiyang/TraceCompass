"""model_run 记录往返与状态机约束。"""

from __future__ import annotations

import json

import pytest

from psios.pipeline import finish_run, get_run, latest_runs, start_run


def test_start_and_finish_run(db) -> None:
    run_id = start_run(db, ["ingest", "chunk"], {"models": {"llm": "none"}})
    run = get_run(db, run_id)
    assert run is not None
    assert run["status"] == "running"
    assert run["started_at"] is not None
    assert json.loads(run["stages"]) == ["ingest", "chunk"]
    assert json.loads(run["config_snapshot"]) == {"models": {"llm": "none"}}

    finish_run(db, run_id, "done", {"ingest": {"messages": 42}})
    run = get_run(db, run_id)
    assert run is not None
    assert run["status"] == "done"
    assert run["finished_at"] is not None
    assert json.loads(run["stats"])["ingest"]["messages"] == 42


def test_finish_requires_valid_status(db) -> None:
    run_id = start_run(db, [], {})
    with pytest.raises(ValueError, match="done/failed"):
        finish_run(db, run_id, "paused")


def test_finish_unknown_run_rejected(db) -> None:
    with pytest.raises(ValueError, match="不存在"):
        finish_run(db, "nope", "done")


def test_failed_run_is_recordable(db) -> None:
    run_id = start_run(db, ["topics"], {})
    finish_run(db, run_id, "failed", {"error": "embedding api down"})
    run = get_run(db, run_id)
    assert run is not None
    assert run["status"] == "failed"


def test_latest_runs_ordered(db) -> None:
    first = start_run(db, ["a"], {})
    second = start_run(db, ["b"], {})
    listed = latest_runs(db, limit=10)
    assert listed[0]["id"] == second
    assert listed[1]["id"] == first
