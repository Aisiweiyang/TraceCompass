"""Personal Model 快照装配(data-model §9):实体表读视图 → JSON 快照落库。"""

from __future__ import annotations

import json
import sqlite3


def build_snapshot(conn: sqlite3.Connection, run_id: str, now: str) -> dict:
    coverage_row = conn.execute(
        "SELECT (SELECT COUNT(*) FROM conversation) AS conversations,"
        " (SELECT COUNT(*) FROM message) AS messages,"
        " (SELECT COUNT(*) FROM chunk) AS chunks,"
        " (SELECT MIN(ts) FROM message) AS date_min,"
        " (SELECT MAX(ts) FROM message) AS date_max"
    ).fetchone()
    sources = [row[0] for row in conn.execute("SELECT name FROM source ORDER BY name")]

    def interests(lifecycle: str | None, limit: int = 20) -> list[dict]:
        rows = conn.execute(
            "SELECT i.topic_id, i.score, i.lifecycle, i.interest_type, t.label"
            " FROM interest i JOIN topic t ON t.id = i.topic_id"
            " WHERE t.status = 'active' AND (:lc IS NULL OR i.lifecycle = :lc)"
            " ORDER BY i.score DESC LIMIT :limit",
            {"lc": lifecycle, "limit": limit},
        ).fetchall()
        return [dict(row) for row in rows]

    def simple_list(table: str, text_col: str) -> list[dict]:
        rows = conn.execute(
            f"SELECT id, status, {text_col} AS text FROM {table}"
            " ORDER BY first_seen, id LIMIT 50"
        ).fetchall()
        return [dict(row) for row in rows]

    payload = {
        "coverage": {
            "conversations": coverage_row["conversations"],
            "messages": coverage_row["messages"],
            "chunks": coverage_row["chunks"],
            "date_min": coverage_row["date_min"],
            "date_max": coverage_row["date_max"],
            "sources": sources,
        },
        "interests": {
            "top": interests(None),
            "emerging": interests("emerging"),
            "stable": interests("stable"),
            "dormant": interests("dormant"),
            "temporary": interests("temporary"),
        },
        "goals": simple_list("goal", "statement"),
        "projects": simple_list("project", "name"),
        "questions": simple_list("question", "display_text"),
    }
    conn.execute(
        "INSERT OR REPLACE INTO personal_model_snapshot (run_id, generated_at, payload)"
        " VALUES (?, ?, ?)",
        (run_id, now, json.dumps(payload, ensure_ascii=False)),
    )
    return payload
