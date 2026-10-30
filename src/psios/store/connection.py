"""SQLite 连接:WAL + 外键强制 + Row 工厂。"""

from __future__ import annotations

import sqlite3
from pathlib import Path


def connect(db_path: Path | str) -> sqlite3.Connection:
    """打开(或创建)数据库连接。

    调用方负责 close();数据目录需已存在(由 cli.init 负责 mkdir)。
    """
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn
