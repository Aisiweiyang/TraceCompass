"""Ingestion:adapter 解析、角色归一化、去重幂等。"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from psios.domain import content_hash
from psios.ingestion import AdapterError, detect_adapter, get_adapter
from psios.ingestion.chatgpt_json import ChatGptJsonAdapter
from psios.ingestion.jsonl import JsonlAdapter
from psios.ingestion.store import import_file, register_source

# ── 构造 ChatGPT 导出样本 ──────────────────────────────────────────────


def _node(role: str | None, text: str | None, create_time: float | None, parent: str | None,
          content_type: str = "text") -> dict:
    message = None
    if role is not None:
        parts: list = []
        if content_type == "multimodal_text":
            parts = [text, {"image_pointer": "file-x.png"}]
        else:
            parts = [text]
        message = {
            "author": {"role": role},
            "create_time": create_time,
            "content": {"content_type": content_type, "parts": parts},
        }
    return {"message": message, "parent": parent}


def _chatgpt_payload() -> list[dict]:
    conv_a = {
        "title": "嵌入式调试",
        "create_time": 1700000000.0,
        "current_node": "n3",
        "mapping": {
            "n1": _node(None, None, None, None),
            "n2": _node("user", "怎么用示波器看 20kHz 信号?", 1700000010.0, "n1"),
            "n3": _node("assistant", "先设置触发模式和时基。", 1700000060.0, "n2"),
        },
    }
    conv_b = {
        "title": "工具调用",
        "create_time": 1700001000.0,
        "current_node": "m3",
        "mapping": {
            "m1": _node(None, None, None, None),
            "m2": _node("user", "今天天气如何?", 1700001010.0, "m1"),
            "m3": _node("tool", '{"temp": 25}', 1700001020.0, "m2"),
        },
    }
    conv_empty = {"title": "空会话", "create_time": None, "current_node": "e1",
                  "mapping": {"e1": _node(None, None, None, None)}}
    return [conv_a, conv_b, conv_empty]


def _write_chatgpt(tmp_path: Path, payload=None, name: str = "conversations.json") -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(payload or _chatgpt_payload(), ensure_ascii=False), encoding="utf-8")
    return path


# ── ChatGPT adapter ────────────────────────────────────────────────────


def test_chatgpt_parse_orders_by_parent_walk(tmp_path: Path) -> None:
    result = ChatGptJsonAdapter().parse(_write_chatgpt(tmp_path))
    assert len(result.conversations) == 2  # 空会话被跳过
    conv = result.conversations[0]
    assert [m.seq for m in conv.messages] == [0, 1]
    assert conv.messages[0].role == "user"
    assert conv.messages[1].role == "assistant"
    assert conv.title == "嵌入式调试"
    assert any("空会话" in w for w in result.warnings)


def test_chatgpt_role_mapping_tool_to_other(tmp_path: Path) -> None:
    result = ChatGptJsonAdapter().parse(_write_chatgpt(tmp_path))
    tool_conv = result.conversations[1]
    assert tool_conv.messages[1].role == "other"
    assert tool_conv.messages[1].meta["source_role"] == "tool"


def test_chatgpt_timestamps_utc_and_tolerant(tmp_path: Path) -> None:
    result = ChatGptJsonAdapter().parse(_write_chatgpt(tmp_path))
    conv = result.conversations[0]
    assert conv.messages[0].ts == datetime.fromtimestamp(1700000010.0, tz=UTC)
    assert conv.started_at is not None and conv.ended_at is not None
    # 空会话 create_time=None 不影响其他会话
    assert result.conversations[0].ended_at == datetime.fromtimestamp(1700000060.0, tz=UTC)


def test_chatgpt_multimodal_parts(tmp_path: Path) -> None:
    payload = [{
        "title": "multimodal", "current_node": "n2",
        "mapping": {
            "n1": _node(None, None, None, None),
            "n2": _node("user", "看这张图 ", 1.0, "n1", content_type="multimodal_text"),
        },
    }]
    result = ChatGptJsonAdapter().parse(_write_chatgpt(tmp_path, payload))
    assert result.conversations[0].messages[0].content == "看这张图 "


def test_chatgpt_external_id_stable_without_conversation_id(tmp_path: Path) -> None:
    first = ChatGptJsonAdapter().parse(_write_chatgpt(tmp_path))
    second = ChatGptJsonAdapter().parse(_write_chatgpt(tmp_path, name="copy.json"))
    ids_first = [c.external_id for c in first.conversations]
    ids_second = [c.external_id for c in second.conversations]
    assert ids_first == ids_second


def test_chatgpt_rejects_non_list(tmp_path: Path) -> None:
    path = tmp_path / "bad.json"
    path.write_text(json.dumps({"just": "a dict"}), encoding="utf-8")
    with pytest.raises(AdapterError):
        ChatGptJsonAdapter().parse(path)


# ── JSONL adapter ──────────────────────────────────────────────────────


def test_jsonl_parse_with_mixed_quality_lines(tmp_path: Path) -> None:
    path = tmp_path / "chat.jsonl"
    lines = [
        json.dumps({"role": "user", "content": "什么是零点漂移?", "ts": "2026-01-05T10:00:00"}),
        "not json at all",
        json.dumps({"role": "wizard", "content": "未知角色", "ts": 1700000000}),
        json.dumps({"role": "assistant", "content": "回答", "ts": "bad-ts"}),
        json.dumps({"no_content": True}),
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")
    result = JsonlAdapter().parse(path)
    conv = result.conversations[0]
    assert len(conv.messages) == 3
    assert conv.messages[0].ts == datetime(2026, 1, 5, 10, 0, tzinfo=UTC)
    assert conv.messages[1].role == "other"  # 未知角色降级
    assert conv.messages[1].ts == datetime.fromtimestamp(1700000000, tz=UTC)
    assert conv.messages[2].ts is None  # 非法时间戳置空
    assert result.stats["skipped_lines"] == 2
    assert result.stats["bad_roles"] == 1
    assert result.stats["bad_ts"] == 1
    assert conv.title == "chat"


def test_jsonl_raises_without_usable_lines(tmp_path: Path) -> None:
    path = tmp_path / "empty.jsonl"
    path.write_text("garbage\n", encoding="utf-8")
    with pytest.raises(AdapterError, match="没有可用"):
        JsonlAdapter().parse(path)


# ── 去重入库 ───────────────────────────────────────────────────────────


def _import(db, path: Path, adapter):
    return import_file(db, path, adapter, "2026-09-10T12:00:00+00:00")


def test_same_file_imported_twice_is_full_skip(db, tmp_path: Path) -> None:
    path = tmp_path / "chat.jsonl"
    path.write_text(
        json.dumps({"role": "user", "content": "第一次"}, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    first = _import(db, path, JsonlAdapter())
    assert first.conversations_inserted == 1
    assert first.messages_inserted == 1

    second = _import(db, path, JsonlAdapter())
    assert second.conversations_inserted == 0
    assert second.messages_inserted == 0
    assert any("整体跳过" in w for w in second.warnings)

    counts = db.execute(
        "SELECT (SELECT COUNT(*) FROM conversation), (SELECT COUNT(*) FROM message)"
    ).fetchone()
    assert tuple(counts) == (1, 1)


def test_same_content_in_renamed_file_skips_conversation(db, tmp_path: Path) -> None:
    line = json.dumps({"role": "user", "content": "重复内容"}, ensure_ascii=False) + "\n"
    a = tmp_path / "a.jsonl"
    b = tmp_path / "b.jsonl"  # 同内容改名 → file_hash 相同 → 整体跳过
    a.write_text(line, encoding="utf-8")
    b.write_text(line, encoding="utf-8")
    _import(db, a, JsonlAdapter())
    second = _import(db, b, JsonlAdapter())
    assert second.conversations_inserted == 0
    assert any("整体跳过" in w for w in second.warnings)


def test_incremental_export_appends_new_conversation(db, tmp_path: Path) -> None:
    """导出 v1(会话 A)→ 导出 v2(会话 A + B):A 不重复,B 新增。"""
    conv = {"title": "A", "current_node": "n2",
            "mapping": {"n1": _node(None, None, None, None),
                        "n2": _node("user", "内容甲", 1.0, "n1")}}
    v1 = _write_chatgpt(tmp_path, [conv], name="v1.json")
    v2 = _write_chatgpt(tmp_path, [conv, {
        "title": "B", "current_node": "n2",
        "mapping": {"n1": _node(None, None, None, None),
                    "n2": _node("user", "内容乙", 2.0, "n1")}}],
        name="v2.json")
    first = _import(db, v1, ChatGptJsonAdapter())
    second = _import(db, v2, ChatGptJsonAdapter())
    assert first.conversations_inserted == 1
    assert second.conversations_inserted == 1  # 只有 B
    assert second.conversations_skipped == 1   # A 原样跳过
    total = db.execute("SELECT COUNT(*) FROM conversation").fetchone()[0]
    assert total == 2


def test_register_source_reuses_same_file_hash(db, tmp_path: Path) -> None:
    path = tmp_path / "s.jsonl"
    path.write_text("{}\n", encoding="utf-8")
    from psios.ingestion.store import file_hash

    sid1, new1 = register_source(db, "s.jsonl", "jsonl", file_hash(path), "now")
    sid2, new2 = register_source(db, "s.jsonl", "jsonl", file_hash(path), "now")
    assert new1 and not new2
    assert sid1 == sid2


# ── 格式识别 ───────────────────────────────────────────────────────────


def test_detect_adapter_by_content(tmp_path: Path) -> None:
    assert isinstance(detect_adapter(_write_chatgpt(tmp_path)), ChatGptJsonAdapter)
    jsonl = tmp_path / "x.jsonl"
    jsonl.write_text('{"role": "user", "content": "hi"}\n', encoding="utf-8")
    assert isinstance(detect_adapter(jsonl), JsonlAdapter)


def test_detect_adapter_rejects_unknown(tmp_path: Path) -> None:
    plain = tmp_path / "plain.json"
    plain.write_text('{"no": "mapping"}', encoding="utf-8")
    with pytest.raises(AdapterError):
        detect_adapter(plain)
    with pytest.raises(AdapterError):
        get_adapter("nonexistent", plain)


def test_content_hash_is_stable() -> None:
    assert content_hash("中文") == content_hash("中文")
    assert content_hash("中文") != content_hash("english")


def test_jsonl_offsets_normalized_to_utc(tmp_path: Path) -> None:
    """带时区偏移的时间戳必须归一到 UTC(下游字符串比较依赖, ENTRY-007)。"""
    path = tmp_path / "tz.jsonl"
    path.write_text(
        json.dumps({"role": "user", "content": "北京时间八点", "ts": "2026-01-01T08:00:00+08:00"})
        + "\n"
        + json.dumps({"role": "user", "content": "UTC 零点", "ts": "2026-01-01T00:00:00Z"})
        + "\n",
        encoding="utf-8",
    )
    result = JsonlAdapter().parse(path)
    messages = result.conversations[0].messages
    first_ts = messages[0].ts
    assert first_ts is not None and first_ts == messages[1].ts  # 同一时刻, 不同写法
    offset = first_ts.utcoffset()
    assert offset is not None and offset.total_seconds() == 0
