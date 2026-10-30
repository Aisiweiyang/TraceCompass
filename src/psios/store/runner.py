"""Migration runner:PRAGMA user_version + 编号 SQL 文件。

规则(data-model.md §12):
- 文件名形如 ``001_init.sql``,按数字前缀升序应用;
- 已应用(user_version ≥ 文件版本)则跳过 → 幂等;
- 每个迁移(含 user_version 提升)在单事务内完成,失败即整体回滚;
- 迁移永不重写历史数据。
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

_PREFIX = re.compile(r"^(\d+)_.*\.sql$")


class MigrationError(RuntimeError):
    """迁移文件非法或执行失败。"""


def migrations_dir() -> Path:
    return Path(__file__).parent / "migrations"


def current_version(conn: sqlite3.Connection) -> int:
    return int(conn.execute("PRAGMA user_version").fetchone()[0])


def _discover(directory: Path) -> list[tuple[int, Path]]:
    found: list[tuple[int, Path]] = []
    for path in directory.glob("*.sql"):
        match = _PREFIX.match(path.name)
        if not match:
            raise MigrationError(f"迁移文件名必须是 '<序号>_<名称>.sql': {path.name}")
        found.append((int(match.group(1)), path))
    versions = [v for v, _ in found]
    if len(versions) != len(set(versions)):
        raise MigrationError(f"迁移序号重复: {sorted(versions)}")
    return sorted(found)


def migrate(conn: sqlite3.Connection, directory: Path | None = None) -> list[str]:
    """应用所有未执行的迁移,返回本次应用的文件名列表。"""
    directory = directory if directory is not None else migrations_dir()
    applied: list[str] = []
    for version, path in _discover(directory):
        if version <= current_version(conn):
            continue
        sql = path.read_text(encoding="utf-8")
        script = f"BEGIN;\n{sql}\nPRAGMA user_version = {version};\nCOMMIT;"
        try:
            conn.executescript(script)
        except sqlite3.Error as exc:
            conn.rollback()
            raise MigrationError(f"迁移 {path.name} 执行失败: {exc}") from exc
        applied.append(path.name)
    return applied
