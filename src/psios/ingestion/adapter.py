"""Adapter 接口、解析结果与格式自动识别。"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path

from psios.domain import Conversation

_JSONL_SUFFIXES = {".jsonl", ".ndjson"}


class AdapterError(ValueError):
    """文件不属于该 adapter 的格式,或解析失败。"""


@dataclass
class ParseResult:
    conversations: list[Conversation] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    stats: dict[str, int] = field(default_factory=dict)
    """计数如 skipped_lines / skipped_parts / empty_conversations,入库时并入 run stats。"""


class Adapter(ABC):
    name: str

    @abstractmethod
    def parse(self, path: Path) -> ParseResult:
        """解析一个文件;永不修改源文件。"""


def _is_chatgpt_payload(payload: object) -> bool:
    """ChatGPT conversations.json:数组(元素含 mapping),或含 conversations 键的根对象。"""
    if isinstance(payload, list):
        return any(isinstance(item, dict) and "mapping" in item for item in payload[:5])
    if isinstance(payload, dict):
        conversations = payload.get("conversations")
        return isinstance(conversations, list)
    return False


def detect_adapter(path: Path) -> Adapter:
    """按扩展名 + 内容嗅探选择 adapter;无法识别时抛 AdapterError。"""
    from psios.ingestion.chatgpt_json import ChatGptJsonAdapter
    from psios.ingestion.jsonl import JsonlAdapter

    suffix = path.suffix.lower()
    if suffix in _JSONL_SUFFIXES:
        return JsonlAdapter()
    if suffix == ".json":
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise AdapterError(f"{path.name}: 不是合法 JSON ({exc})") from exc
        if _is_chatgpt_payload(payload):
            return ChatGptJsonAdapter()
        raise AdapterError(
            f"{path.name}: JSON 内容无法识别(ChatGPT conversations.json 应为含 mapping 的数组)"
        )
    raise AdapterError(f"{path.name}: 不支持的扩展名 {suffix!r}(支持 .json / .jsonl)")


def get_adapter(name: str, path: Path) -> Adapter:
    """按名字取 adapter;'auto' 走嗅探。"""
    from psios.ingestion.chatgpt_json import ChatGptJsonAdapter
    from psios.ingestion.jsonl import JsonlAdapter

    by_name: dict[str, type[Adapter]] = {
        "auto": type(detect_adapter(path)),
        "chatgpt_json": ChatGptJsonAdapter,
        "jsonl": JsonlAdapter,
    }
    if name not in by_name:
        raise AdapterError(f"未知 adapter {name!r}(可选: auto / chatgpt_json / jsonl)")
    return by_name[name]()
