"""CLI:python -m psios init/status/invariants 端到端。"""

from __future__ import annotations

from pathlib import Path

import pytest

from psios.cli import main
from psios.store import connect, current_version, invariants


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


def test_init_creates_database(tmp_path: Path, config_file: Path) -> None:
    assert main(["init", "--config", str(config_file)]) == 0
    assert (tmp_path / "data" / "psios.db").is_file()


def test_init_is_rerunnable(tmp_path: Path, config_file: Path) -> None:
    assert main(["init", "--config", str(config_file)]) == 0
    assert main(["init", "--config", str(config_file)]) == 0  # 幂等


def test_init_with_bad_config_fails(tmp_path: Path) -> None:
    bad = tmp_path / "bad.toml"
    bad.write_text("[interest_weights]\n", encoding="utf-8")
    assert main(["init", "--config", str(bad)]) == 1


def test_status_and_invariants(tmp_path: Path, config_file: Path, capsys) -> None:
    main(["init", "--config", str(config_file)])
    assert main(["status", "--config", str(config_file)]) == 0
    assert "schema version: 2" in capsys.readouterr().out
    assert main(["invariants", "--config", str(config_file)]) == 0
    assert "全部通过" in capsys.readouterr().out


def test_status_without_db_fails(tmp_path: Path, config_file: Path) -> None:
    assert main(["status", "--config", str(config_file)]) == 1


def test_invariants_detect_violation_via_cli(tmp_path: Path, config_file: Path, capsys) -> None:
    main(["init", "--config", str(config_file)])
    db_path = tmp_path / "data" / "psios.db"
    conn = connect(db_path)
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute(
        "INSERT INTO model_run (id, config_snapshot, status, started_at) "
        "VALUES ('r1','{}','running','2026-09-08T00:00:00')"
    )
    conn.execute(
        "INSERT INTO source VALUES ('s1','n','jsonl','2026-09-08T00:00:00',NULL,NULL)"
    )
    conn.execute(
        "INSERT INTO conversation VALUES ('c1','s1','e1',NULL,NULL,NULL,NULL,'h1',NULL,"
        "'2026-09-08T00:00:00')"
    )
    conn.execute(
        "INSERT INTO message VALUES ('m1','c1',1,'user','内容','2026-09-08T00:00:00','h2',"
        "NULL,'2026-09-08T00:00:00')"
    )
    conn.execute(
        "INSERT INTO topic (id, label, label_source, inference_method, status, created_at,"
        " updated_at) VALUES ('t1','L','keyword','m','active','2026-09-08T00:00:00',"
        "'2026-09-08T00:00:00')"
    )
    conn.execute(
        "INSERT INTO interest (topic_id, inference_method, computed_at, updated_at) "
        "VALUES ('t1','m','2026-09-08T00:00:00','2026-09-08T00:00:00')"
    )
    conn.commit()
    conn.close()

    assert main(["invariants", "--config", str(config_file)]) == 1
    assert "evidence" in capsys.readouterr().err


def test_project_own_config_and_db(tmp_path: Path, project_root: Path) -> None:
    """真实项目 config + 真实 init(M0 产出验收)。"""
    config = project_root / "config.toml"
    db_override = tmp_path / "real.db"
    assert main(["init", "--config", str(config), "--db", str(db_override)]) == 0
    conn = connect(db_override)
    try:
        assert current_version(conn) == 2
        invariants.assert_no_violations(conn)
    finally:
        conn.close()
