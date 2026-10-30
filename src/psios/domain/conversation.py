"""Conversation Schema(data-model.md §2)—— 所有 adapter 的统一输出。

id / source_id 由入库层(ingestion.store)以 uuid5 内容寻址方式赋值,
adapter 产出的 Conversation 这两个字段为 None;重复导入因此天然幂等。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime

ROLES = ("user", "assistant", "system", "other")


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass
class Message:
    seq: int
    role: str
    content: str
    ts: datetime | None = None
    meta: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.role not in ROLES:
            raise ValueError(f"role 必须是 {ROLES} 之一, 收到 {self.role!r}")

    @property
    def content_hash(self) -> str:
        return content_hash(self.content)


@dataclass
class Conversation:
    external_id: str
    title: str | None = None
    started_at: datetime | None = None
    ended_at: datetime | None = None
    participants: list[str] = field(default_factory=list)
    messages: list[Message] = field(default_factory=list)
    meta: dict = field(default_factory=dict)
    id: str | None = None          # 入库时赋值
    source_id: str | None = None   # 入库时赋值

    def __post_init__(self) -> None:
        if not self.external_id:
            raise ValueError("external_id 不能为空(缺失时 adapter 必须以内容哈希代替)")

    def message_hashes(self) -> str:
        """会话级内容哈希的基础:全部消息哈希的连接。"""
        return "|".join(m.content_hash for m in self.messages)

    def infer_time_range(self) -> tuple[datetime | None, datetime | None]:
        """started/ended 缺失时从消息时间戳推断;全缺失返回 (None, None)。"""
        start = self.started_at
        end = self.ended_at
        stamps = [m.ts for m in self.messages if m.ts is not None]
        if start is None and stamps:
            start = min(stamps)
        if end is None and stamps:
            end = max(stamps)
        return start, end
