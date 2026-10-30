"""ChatGPT 导出(conversations.json)adapter。

预期结构(公开已知格式,2026-09 时点;**尚未用真实导出文件验证**,见 research-log):
[ { "title": ..., "create_time": epoch, "update_time": epoch,
    "current_node": "<id>",
    "mapping": { "<node_id>": { "message": {"author": {"role": ...},
                                            "create_time": epoch,
                                            "content": {"content_type": "text",
                                                        "parts": [str, ...]}},
                                "parent": "<node_id>"|None, ... }, ... } ]

时间戳缺失/非法 → None(时间分析降级是 schema 允许的);tool 角色 → other。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from psios.domain import Conversation, Message, content_hash
from psios.ingestion.adapter import Adapter, AdapterError, ParseResult

_ROLE_MAP = {"user": "user", "assistant": "assistant", "system": "system"}


def _epoch_to_dt(value: Any) -> datetime | None:
    if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
        return None
    try:
        return datetime.fromtimestamp(value, tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None


def _extract_text(content: dict[str, Any]) -> str | None:
    """content.parts 中的字符串拼接;multimodal 的非文本部分跳过并计数。"""
    if not isinstance(content, dict):
        return None
    parts = content.get("parts")
    if not isinstance(parts, list):
        return None
    texts: list[str] = []
    for part in parts:
        if isinstance(part, str):
            texts.append(part)
    joined = "".join(texts)
    return joined if joined.strip() else None


def _walk_messages(mapping: dict[str, Any], current_node: Any) -> tuple[list[dict[str, Any]], int]:
    """从 current_node 沿 parent 指针回溯到根,再反转为时间顺序。"""
    chain: list[dict[str, Any]] = []
    seen: set[str] = set()
    node_id: Any = current_node
    skipped = 0
    while isinstance(node_id, str) and node_id not in seen:
        seen.add(node_id)
        node = mapping.get(node_id)
        if not isinstance(node, dict):
            skipped += 1
            break
        message = node.get("message")
        if isinstance(message, dict):
            chain.append(message)
        node_id = node.get("parent")
    chain.reverse()
    return chain, skipped


def _parse_conversation(raw: Any, warnings: list[str]) -> Conversation | None:
    if not isinstance(raw, dict):
        return None
    mapping = raw.get("mapping")
    if not isinstance(mapping, dict):
        return None

    ordered, skipped_nodes = _walk_messages(mapping, raw.get("current_node"))
    messages: list[Message] = []
    seq = 0
    for message in ordered:
        role_raw = str((message.get("author") or {}).get("role", "other"))
        role = _ROLE_MAP.get(role_raw, "other")
        content = message.get("content")
        if not isinstance(content, dict):
            continue
        text = _extract_text(content)
        if text is None:
            continue
        messages.append(
            Message(
                seq=seq,
                role=role,
                content=text,
                ts=_epoch_to_dt(message.get("create_time")),
                meta={"source_role": role_raw},
            )
        )
        seq += 1

    if not messages:
        return None

    title = raw.get("title")
    external_id = str(raw.get("conversation_id") or "")
    if not external_id:
        # 无 id 的导出:以 title + 内容哈希派生稳定 external_id(重导同一数据不变)
        basis = f"{title or ''}|{messages[0].content_hash}|{len(messages)}"
        external_id = "chatgpt-" + content_hash(basis)[:16]
    if skipped_nodes:
        warnings.append(f"conversation {external_id[:24]}…: {skipped_nodes} 个坏节点被跳过")

    conversation = Conversation(
        external_id=external_id,
        title=str(title) if title else None,
        participants=[],
        messages=messages,
        meta={"adapter": "chatgpt_json"},
    )
    conversation.started_at, conversation.ended_at = conversation.infer_time_range()
    return conversation


class ChatGptJsonAdapter(Adapter):
    name = "chatgpt_json"

    def parse(self, path: Path) -> ParseResult:
        result = ParseResult()
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise AdapterError(f"{path.name}: JSON 解析失败 ({exc})") from exc

        items = payload
        if isinstance(payload, dict):  # 兼容 {conversations: [...]} 包装
            items = payload.get("conversations")
        if not isinstance(items, list):
            raise AdapterError(f"{path.name}: 预期 JSON 数组, 收到 {type(payload).__name__}")

        empty = 0
        for raw in items:
            conversation = _parse_conversation(raw, result.warnings)
            if conversation is None:
                empty += 1
                continue
            result.conversations.append(conversation)
        if empty:
            result.warnings.append(f"{path.name}: {empty} 个空会话(无可用消息)被跳过")
        result.stats["empty_conversations"] = empty
        return result
