"""CLI import/dump 端到端 + 幂等验收(DoD #1/#2)。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from psios.cli import main
from psios.store import connect


@pytest.fixture()
def config_file(tmp_path: Path) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(
        '[storage]\ndb_path = "data/psios.db"\n\n'
        "[interest_weights]\n"
        "frequency = 0.15\nrecency = 0.10\npersistence = 0.20\ndepth = 0.10\n"
        "return_rate = 0.15\nfollowup_density = 0.05\nproject_conversion = 0.10\n"
        "connectivity = 0.05\nvoluntary = 0.10\n",
        encoding="utf-8",
    )
    return path


@pytest.fixture()
def raw_dir(tmp_path: Path) -> Path:
    """一个 ChatGPT 导出 + 一个 JSONL,含中文内容。"""
    data = tmp_path / "raw"
    data.mkdir()
    chatgpt = [{
        "title": "个人知识系统",
        "current_node": "n3",
        "mapping": {
            "n1": {"message": None, "parent": None},
            "n2": {"message": {"author": {"role": "user"}, "create_time": 1700000000.0,
                               "content": {"content_type": "text",
                                           "parts": ["Obsidian 和 Logseq 怎么选?"]}},
                    "parent": "n1"},
            "n3": {"message": {"author": {"role": "assistant"}, "create_time": 1700000100.0,
                               "content": {"content_type": "text",
                                           "parts": ["取决于你是否需要双向链接。"]}},
                    "parent": "n2"},
        },
    }]
    (data / "conversations.json").write_text(
        json.dumps(chatgpt, ensure_ascii=False), encoding="utf-8"
    )
    (data / "notes.jsonl").write_text(
        json.dumps({"role": "user", "content": "智能车电磁组的 20kHz 是怎么来的?",
                    "ts": "2026-03-01T09:30:00"}, ensure_ascii=False) + "\n"
        + json.dumps({"role": "assistant", "content": "赛题规定的信号频率。",
                      "ts": "2026-03-01T09:31:00"}, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return data


def _table_counts(db_path: Path) -> tuple[int, int]:
    conn = connect(db_path)
    try:
        row = conn.execute(
            "SELECT (SELECT COUNT(*) FROM conversation), (SELECT COUNT(*) FROM message)"
        ).fetchone()
        return int(row[0]), int(row[1])
    finally:
        conn.close()


def test_import_dir_end_to_end(tmp_path: Path, config_file: Path, raw_dir: Path, capsys) -> None:
    assert main(["init", "--config", str(config_file)]) == 0
    db_path = tmp_path / "data" / "psios.db"

    assert main(["import", str(raw_dir), "--config", str(config_file)]) == 0
    out = capsys.readouterr().out
    assert "[chatgpt_json] conversations.json" in out
    assert "[jsonl] notes.jsonl" in out

    assert _table_counts(db_path) == (2, 4)  # 2 会话: 2+2 条消息

    # model_run 记录为 done
    conn = connect(db_path)
    try:
        run = conn.execute("SELECT status, stages FROM model_run").fetchone()
        assert run[0] == "done"
        assert json.loads(run[1]) == ["ingest"]
    finally:
        conn.close()

    # dump 可检查中间产物
    assert main(["dump", "conversations", "--config", str(config_file)]) == 0
    out = capsys.readouterr().out
    assert "个人知识系统" in out
    assert "notes" in out
    assert main(["dump", "messages", "--config", str(config_file)]) == 0
    assert "Obsidian 和 Logseq 怎么选?" in capsys.readouterr().out


def test_import_is_idempotent(tmp_path: Path, config_file: Path, raw_dir: Path) -> None:
    main(["init", "--config", str(config_file)])
    db_path = tmp_path / "data" / "psios.db"

    assert main(["import", str(raw_dir), "--config", str(config_file)]) == 0
    counts_first = _table_counts(db_path)
    assert main(["import", str(raw_dir), "--config", str(config_file)]) == 0
    assert _table_counts(db_path) == counts_first  # 幂等验收: 行数不变


def test_import_failure_rolls_back_and_records_failed_run(
    tmp_path: Path, config_file: Path, raw_dir: Path, capsys
) -> None:
    main(["init", "--config", str(config_file)])
    db_path = tmp_path / "data" / "psios.db"
    (raw_dir / "broken.json").write_text('{"not": "chatgpt"}', encoding="utf-8")

    assert main(["import", str(raw_dir), "--config", str(config_file)]) == 1
    assert "导入失败" in capsys.readouterr().err
    # 事务回滚: notes.jsonl 的数据也不应留下
    assert _table_counts(db_path) == (0, 0)
    conn = connect(db_path)
    try:
        runs = conn.execute("SELECT status FROM model_run").fetchall()
        assert [r[0] for r in runs] == ["failed"]
    finally:
        conn.close()


def test_import_missing_path_fails(tmp_path: Path, config_file: Path) -> None:
    main(["init", "--config", str(config_file)])
    assert main(["import", str(tmp_path / "nope"), "--config", str(config_file)]) == 1


def test_import_without_db_fails(tmp_path: Path, config_file: Path, raw_dir: Path, capsys) -> None:
    assert main(["import", str(raw_dir), "--config", str(config_file)]) == 1
    assert "数据库不存在" in capsys.readouterr().err


def test_forced_adapter(tmp_path: Path, config_file: Path, raw_dir: Path, capsys) -> None:
    main(["init", "--config", str(config_file)])
    args = ["import", str(raw_dir / "notes.jsonl"), "--adapter", "jsonl",
            "--config", str(config_file)]
    assert main(args) == 0
    assert "[jsonl] notes.jsonl" in capsys.readouterr().out
