"""通用 JSONL adapter:每行 {role, content, ts?},一个文件 = 一个 conversation。

ts 支持 ISO8601 字符串或 epoch 秒数;role 必须是
user/assistant/system/other(未知角色降级为 other 并告警)。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from psios.domain import ROLES, Conversation, Message, content_hash
from psios.ingestion.adapter import Adapter, AdapterError, ParseResult


def _parse_ts(value: Any) -> datetime | None:
    """所有时间统一为 UTC(aware)——下游 ISO 字符串比较/排序依赖这一点。"""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if value <= 0:
            return None
        try:
            return datetime.fromtimestamp(value, tz=UTC)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=UTC)  # 无时区视为 UTC
        return parsed.astimezone(UTC)  # 带偏移(如 +08:00)归一到 UTC
    return None


class JsonlAdapter(Adapter):
    name = "jsonl"

    def parse(self, path: Path) -> ParseResult:
        result = ParseResult()
        messages: list[Message] = []
        skipped_lines = 0
        bad_roles = 0
        bad_ts = 0

        for raw_line in path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                skipped_lines += 1
                continue
            if not isinstance(row, dict) or not isinstance(row.get("content"), str):
                skipped_lines += 1
                continue
            role = str(row.get("role", "other"))
            if role not in ROLES:
                role = "other"
                bad_roles += 1
            ts = _parse_ts(row.get("ts"))
            if row.get("ts") is not None and ts is None:
                bad_ts += 1
            messages.append(Message(seq=len(messages), role=role, content=row["content"], ts=ts))

        if not messages:
            raise AdapterError(f"{path.name}: 没有可用的消息行")
        if skipped_lines:
            result.warnings.append(f"{path.name}: {skipped_lines} 个坏行被跳过")
        if bad_roles:
            result.warnings.append(f"{path.name}: {bad_roles} 行未知 role 降级为 other")
        if bad_ts:
            result.warnings.append(f"{path.name}: {bad_ts} 行非法时间戳置空")

        result.stats = {
            "skipped_lines": skipped_lines,
            "bad_roles": bad_roles,
            "bad_ts": bad_ts,
        }
        external_id = "jsonl-" + content_hash(
            path.name + "|" + "|".join(m.content_hash for m in messages)
        )[:16]
        conversation = Conversation(
            external_id=external_id,
            title=path.stem,
            participants=[],
            messages=messages,
            meta={"adapter": "jsonl"},
        )
        conversation.started_at, conversation.ended_at = conversation.infer_time_range()
        result.conversations.append(conversation)
        return result
