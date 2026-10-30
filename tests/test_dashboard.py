"""Dashboard:页面渲染、人工修正端点、mute 语义、merge、404。真实 HTTP 端到端。"""

from __future__ import annotations

import threading
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import pytest

from psios.ids import stable_id
from psios.store import connect, migrate
from psios.web import make_server

NOW = "2026-09-13T12:00:00+00:00"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None  # 不跟随重定向,保留 303 供断言


_OPENER = urllib.request.build_opener(_NoRedirect)


@pytest.fixture()
def served_db(tmp_path: Path):
    """文件库(服务与断言共用同一路径),含可评分主题 + 证据 + goal。"""
    conn = connect(tmp_path / "dashboard.db")
    migrate(conn)
    conn.execute(
        "INSERT INTO model_run (id, config_snapshot, status, started_at)"
        " VALUES ('r0','{}','done','2026-01-01T00:00:00')"
    )
    conn.execute(
        "INSERT INTO source VALUES ('s1','n','jsonl','2026-01-01T00:00:00',NULL,NULL)"
    )
    conn.execute(
        "INSERT INTO conversation (id, source_id, external_id, content_hash, created_at)"
        " VALUES ('c1','s1','e1','h','2026-01-01T00:00:00')"
    )
    conn.execute(
        "INSERT INTO message (id, conversation_id, seq, role, content, ts, content_hash,"
        " meta, created_at) VALUES ('m1','c1',0,'user','Obsidian 知识管理笔记',"
        "'2026-05-01T10:00:00','h','{}','2026-01-01T00:00:00')"
    )
    chunk_id = stable_id("chunk", "c1", "0", "h1")
    conn.execute(
        "INSERT INTO chunk (id, conversation_id, message_id, seq, text, token_count,"
        " created_at) VALUES (?,'c1','m1',0,'Obsidian 知识管理笔记',9,'2026-01-01T00:00:00')",
        (chunk_id,),
    )
    conn.execute(
        "INSERT INTO topic (id, label, label_source, inference_method, status, created_at,"
        " updated_at) VALUES ('t1','知识管理','keyword','leader_cluster','active',?,?)",
        (NOW, NOW),
    )
    conn.execute(
        "INSERT INTO topic_chunk (topic_id, chunk_id, weight, method) VALUES"
        " ('t1', ?, 1.0, 'leader')",
        (chunk_id,),
    )
    conn.execute(
        "INSERT INTO interest (topic_id, score, scores, raw_stats, interest_type,"
        " lifecycle, confidence, inference_method, model_version, computed_at, updated_at)"
        " VALUES ('t1', 0.62, '{}', '{}', 'intrinsic', 'stable', 0.7,"
        " 'signal_rules', 'v0.1', ?, ?)",
        (NOW, NOW),
    )
    conn.execute(
        "INSERT INTO evidence (id, subject_type, subject_id, message_id, chunk_id, quote,"
        " signal_type, created_at) VALUES ('ev1','interest','t1','m1',?,"
        "'Obsidian 知识管理笔记','signal:frequency',?)",
        (chunk_id, NOW),
    )
    conn.execute(
        "INSERT INTO goal (id, statement, kind, epistemic_status, status, confidence,"
        " inference_method, created_at, updated_at) VALUES ('g1','年底上线 V0.1',"
        " 'explicit','USER_STATED','active',0.6,'rule_regex',?,?)",
        (NOW, NOW),
    )
    conn.execute(
        "INSERT INTO evidence (id, subject_type, subject_id, message_id, quote,"
        " signal_type, created_at) VALUES ('ev-g1','goal','g1','m1',"
        "'我的目标是在年底前上线 V0.1','goal_marker',?)",
        (NOW,),
    )
    conn.commit()
    yield conn
    conn.close()


@pytest.fixture()
def base_url(served_db, tmp_path: Path):
    db_path = str(tmp_path / "dashboard.db")
    server = make_server(db_path, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


def _get(url: str) -> tuple[int, str]:
    try:
        with urllib.request.urlopen(url) as response:
            return response.status, response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8")


def _post(url: str, fields: dict[str, str]) -> tuple[int, str]:
    """POST,不跟随重定向;返回 (status, location)。"""
    data = urllib.parse.urlencode(fields).encode()
    request = urllib.request.Request(url, data=data, method="POST")
    try:
        with _OPENER.open(request) as response:
            return response.status, response.headers.get("Location", "")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers.get("Location", "")


def test_pages_render(base_url: str) -> None:
    status, body = _get(base_url + "/")
    assert status == 200
    assert "Personal Overview" in body and "知识管理" in body and "0.620" in body

    for path, marker in [
        ("/goals", "年底上线"), ("/projects", ""), ("/questions", ""),
        ("/meta-interests", "Meta"), ("/runs", "Pipeline"), ("/evolution", "Topic Evolution"),
    ]:
        status, body = _get(base_url + path)
        assert status == 200, path
        if marker:
            assert marker in body, path

    status, body = _get(f"{base_url}/interest/t1")
    assert status == 200
    assert "总分" in body
    assert "signal:frequency" in body and "原文" in body
    assert "状态历史" in body

    status, body = _get(f"{base_url}/message/m1")
    assert status == 200 and "Obsidian 知识管理笔记" in body

    status, body = _get(f"{base_url}/runs/r0")
    assert status == 200 and "r0" in body


def test_404_and_missing_interest(base_url: str) -> None:
    assert _get(base_url + "/no-such-page")[0] == 404
    status, body = _get(f"{base_url}/interest/ghost")
    assert status == 200 and "未找到该主题" in body


def test_edit_label_writes_revision_and_human_action(base_url: str, served_db) -> None:
    status, location = _post(f"{base_url}/topics/t1/label", {"label": "个人知识体系"})
    assert status == 303 and location.endswith("/interest/t1")
    row = served_db.execute("SELECT label, label_source FROM topic WHERE id='t1'").fetchone()
    assert row["label"] == "个人知识体系" and row["label_source"] == "human"
    assert served_db.execute(
        "SELECT COUNT(*) FROM human_action WHERE action_type = 'edit_label'"
    ).fetchone()[0] == 1
    assert served_db.execute(
        "SELECT COUNT(*) FROM entity_revision WHERE reason = 'human_edit_label'"
    ).fetchone()[0] == 1


def test_mute_and_unmute_toggle_visibility(base_url: str, served_db) -> None:
    _post(f"{base_url}/interests/t1/mute", {})
    _, body = _get(base_url + "/")
    assert "知识管理" not in body and "已隐藏 1 项" in body
    # 详情页仍可直接访问(隐藏 ≠ 删除)
    assert _get(f"{base_url}/interest/t1")[0] == 200
    # unmute 后恢复
    _post(f"{base_url}/interests/t1/unmute", {})
    _, body = _get(base_url + "/")
    assert "知识管理" in body
    assert served_db.execute(
        "SELECT COUNT(*) FROM human_action WHERE action_type IN"
        " ('mute_interest','unmute_interest')"
    ).fetchone()[0] == 2


def test_merge_moves_chunks_and_records(base_url: str, served_db) -> None:
    served_db.execute(
        "INSERT INTO topic (id, label, label_source, inference_method, status, created_at,"
        " updated_at) VALUES ('t2','另一个主题','keyword','leader_cluster','active',?,?)",
        (NOW, NOW),
    )
    served_db.commit()
    status, location = _post(f"{base_url}/topics/merge", {"from": "t1", "to": "t2"})
    assert status == 303 and location.endswith("/interest/t2")
    assert served_db.execute(
        "SELECT status, merged_into_id FROM topic WHERE id='t1'"
    ).fetchone()["merged_into_id"] == "t2"
    assert served_db.execute(
        "SELECT COUNT(*) FROM topic_chunk WHERE topic_id='t2'"
    ).fetchone()[0] == 1

    from psios.store import invariants

    invariants.assert_no_violations(served_db)


def test_confirm_goal_bumps_confidence(base_url: str, served_db) -> None:
    status, _ = _post(f"{base_url}/goals/g1/confirm", {})
    assert status == 303
    assert served_db.execute(
        "SELECT confidence FROM goal WHERE id='g1'"
    ).fetchone()["confidence"] >= 0.95
