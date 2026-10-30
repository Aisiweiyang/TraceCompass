"""model_run 记录:每次 pipeline 执行的 manifest(data-model.md §8)。"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import UTC, datetime
from typing import Any

from psios import __version__


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def code_version() -> str:
    """版本串;后续接入 git hash 时仅改此处。"""
    return f"psios {__version__}"


def start_run(
    conn: sqlite3.Connection,
    stages: list[str],
    config_snapshot: dict[str, Any],
) -> str:
    run_id = uuid.uuid4().hex
    conn.execute(
        "INSERT INTO model_run (id, stages, config_snapshot, code_version, status, started_at) "
        "VALUES (?, ?, ?, ?, 'running', ?)",
        (run_id, json.dumps(stages), json.dumps(config_snapshot, ensure_ascii=False),
         code_version(), _now()),
    )
    return run_id


def finish_run(
    conn: sqlite3.Connection,
    run_id: str,
    status: str,
    stats: dict[str, Any] | None = None,
) -> None:
    if status not in ("done", "failed"):
        raise ValueError(f"status 必须是 done/failed, 收到 {status!r}")
    cursor = conn.execute(
        "UPDATE model_run SET status = ?, stats = ?, finished_at = ? WHERE id = ?",
        (status, json.dumps(stats or {}, ensure_ascii=False), _now(), run_id),
    )
    if cursor.rowcount == 0:
        raise ValueError(f"model_run 不存在: {run_id}")


def get_run(conn: sqlite3.Connection, run_id: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM model_run WHERE id = ?", (run_id,)).fetchone()
    return dict(row) if row else None


def latest_runs(conn: sqlite3.Connection, limit: int = 10) -> list[dict[str, Any]]:
    # 秒级时间戳会同秒碰撞,追加 rowid 保证插入序稳定
    rows = conn.execute(
        "SELECT id, stages, status, started_at, finished_at FROM model_run "
        "ORDER BY started_at DESC, rowid DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [dict(row) for row in rows]
