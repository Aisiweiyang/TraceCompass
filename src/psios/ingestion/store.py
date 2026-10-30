"""入库层:source 登记与 conversation/message 幂等写入。

幂等机制(不变量 #1):
- 实体 ID = uuid5(内容寻址):同一数据重复导入得到相同主键;
- 写入一律 INSERT OR IGNORE;
- source 以整文件 SHA256 去重 —— 同一文件第二次导入直接跳过。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from psios.domain import Conversation
from psios.ids import stable_id as _uuid5
from psios.ingestion.adapter import Adapter, ParseResult


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat(timespec="seconds") if dt is not None else None


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


@dataclass
class ImportStats:
    conversations_inserted: int = 0
    conversations_skipped: int = 0
    messages_inserted: int = 0
    messages_skipped: int = 0
    warnings: list[str] = field(default_factory=list)
    extra: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict[str, int]:
        return {
            "conversations_inserted": self.conversations_inserted,
            "conversations_skipped": self.conversations_skipped,
            "messages_inserted": self.messages_inserted,
            "messages_skipped": self.messages_skipped,
            **self.extra,
        }


def register_source(
    conn: sqlite3.Connection,
    name: str,
    adapter: str,
    digest: str,
    imported_at: str,
) -> tuple[str, bool]:
    """登记来源文件;返回 (source_id, 是否为新 source)。同 file_hash → 复用旧 source。"""
    source_id = _uuid5("source", name, digest)
    row = conn.execute(
        "SELECT id FROM source WHERE id = ? OR (file_hash = ? AND file_hash IS NOT ?)",
        (source_id, digest, None),
    ).fetchone()
    if row is not None:
        return str(row[0]), False
    conn.execute(
        "INSERT INTO source (id, name, adapter, imported_at, file_hash) VALUES (?,?,?,?,?)",
        (source_id, name, adapter, imported_at, digest),
    )
    return source_id, True


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False)


def _store_conversation(
    conn: sqlite3.Connection,
    source_id: str,
    conversation: Conversation,
) -> tuple[bool, int, int]:
    """写单个会话;返回 (会话是否新建, 消息插入数, 消息跳过数)。"""
    assert conversation.id is not None
    cursor = conn.execute(
        "INSERT OR IGNORE INTO conversation (id, source_id, external_id, title, started_at,"
        " ended_at, participants, content_hash, meta, created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,strftime('%Y-%m-%dT%H:%M:%SZ','now'))",
        (
            conversation.id,
            source_id,
            conversation.external_id,
            conversation.title,
            _iso(conversation.started_at),
            _iso(conversation.ended_at),
            _json(conversation.participants),
            _json(conversation.message_hashes()),
            _json(conversation.meta),
        ),
    )
    created = cursor.rowcount > 0
    inserted = 0
    skipped = 0
    for message in conversation.messages:
        message_id = _uuid5("message", conversation.id, str(message.seq), message.content_hash)
        cursor = conn.execute(
            "INSERT OR IGNORE INTO message (id, conversation_id, seq, role, content, ts,"
            " content_hash, meta, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,strftime('%Y-%m-%dT%H:%M:%SZ','now'))",
            (
                message_id,
                conversation.id,
                message.seq,
                message.role,
                message.content,
                _iso(message.ts),
                message.content_hash,
                _json(message.meta),
            ),
        )
        if cursor.rowcount > 0:
            inserted += 1
        else:
            skipped += 1
    return created, inserted, skipped


def import_file(
    conn: sqlite3.Connection,
    path: Path,
    adapter: Adapter,
    now: str,
) -> ImportStats:
    """解析并入库一个文件;返回统计(含 parser 警告)。整体调用方负责事务。"""
    digest = file_hash(path)
    source_id, is_new = register_source(conn, path.name, adapter.name, digest, now)
    stats = ImportStats()

    if not is_new:
        stats.warnings.append(f"{path.name}: 同一文件已导入过, 整体跳过(file_hash 命中)")
        return stats

    parsed: ParseResult = adapter.parse(path)
    stats.extra.update(parsed.stats)
    stats.warnings.extend(parsed.warnings)

    for conversation in parsed.conversations:
        conversation.source_id = source_id
        # 会话身份 = 内容身份(external_id 由 adapter 从内容派生):
        # 同一会话经不同批次导出文件再导入必须命中同一行(DoD #13)。
        # source_id 只记录"最早来源",不参与身份。
        conversation.id = _uuid5("conversation", conversation.external_id)
        created, msg_in, msg_skip = _store_conversation(conn, source_id, conversation)
        stats.conversations_inserted += int(created)
        stats.conversations_skipped += int(not created)
        stats.messages_inserted += msg_in
        stats.messages_skipped += msg_skip
    return stats
