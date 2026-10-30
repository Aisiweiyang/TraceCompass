"""共享 fixture:迁移完成的临时库与项目配置。"""

from __future__ import annotations

from pathlib import Path

import pytest

from psios.config import load_config
from psios.store import connect, migrate

PROJECT_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture()
def db(tmp_path: Path):
    """已迁移到最新 schema 的临时文件库。"""
    path = tmp_path / "test.db"
    conn = connect(path)
    migrate(conn)
    yield conn
    conn.close()


@pytest.fixture()
def project_config():
    """项目自带 config.toml 必须始终合法。"""
    return load_config(PROJECT_ROOT / "config.toml")


@pytest.fixture()
def project_root() -> Path:
    return PROJECT_ROOT
