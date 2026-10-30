"""chunk 化 stage:message → chunk(确定性、幂等)。

- 单条消息 ≲ 500 token(估算)时整体一块;超长按句边界切,保留一句重叠;
  超长单句按字符窗口硬切。
- chunk.id = uuid5(内容寻址)→ 重复执行 INSERT OR IGNORE,天然幂等。
"""

from __future__ import annotations

import re
import sqlite3

from psios.ids import stable_id

MAX_TOKENS = 500
_OVERLAP_SENTENCES = 1

_SENTENCE_END = re.compile(r"[。!!??\n]")
_CJK = re.compile(r"[\u4e00-\u9fff]")


def estimate_tokens(text: str) -> int:
    """粗估:CJK 字符 ≈ 1 token,其余 ≈ 4 字符 1 token。仅用于切分预算,非精确计数。"""
    cjk = len(_CJK.findall(text))
    other = len(text) - cjk
    return max(1, cjk + (other + 3) // 4)


def _sentences(text: str) -> list[str]:
    parts: list[str] = []
    buf: list[str] = []
    for ch in text:
        buf.append(ch)
        if _SENTENCE_END.match(ch):
            parts.append("".join(buf))
            buf = []
    if buf:
        parts.append("".join(buf))
    return [p for p in (s.strip() for s in parts) if p]


def split_text(text: str, max_tokens: int = MAX_TOKENS) -> list[str]:
    """把文本切成 ≤ max_tokens 的块;超长单句按字符窗口硬切。

    保证: 每块预算 ≤ max_tokens(重叠句放不下时丢弃重叠,不牺牲预算)。
    """
    if not text.strip():
        return []
    if estimate_tokens(text) <= max_tokens:
        return [text]

    # 超长单句:硬切(窗口 ≈ max_tokens 字符,对 CJK 近似成立)
    def hard_split(sentence: str) -> list[str]:
        window = max(32, max_tokens)
        return [sentence[i:i + window] for i in range(0, len(sentence), window)]

    sentences: list[str] = []
    for sentence in _sentences(text):
        if estimate_tokens(sentence) <= max_tokens:
            sentences.append(sentence)
        else:
            sentences.extend(hard_split(sentence))

    chunks: list[str] = []
    buf: list[str] = []
    buf_tokens = 0
    for sentence in sentences:
        cost = estimate_tokens(sentence)
        if buf and buf_tokens + cost > max_tokens:
            chunks.append("".join(buf))
            overlap = buf[-_OVERLAP_SENTENCES:]
            buf_tokens = sum(estimate_tokens(s) for s in overlap)
            # 重叠句 + 新句仍超预算 → 放弃重叠,保证预算不破
            buf = list(overlap) if buf_tokens + cost <= max_tokens else []
            if not buf:
                buf_tokens = 0
        buf.append(sentence)
        buf_tokens += cost
    if buf:
        chunks.append("".join(buf))
    return [c for c in (chunk.strip() for chunk in chunks) if c]


def run_chunk_stage(conn: sqlite3.Connection) -> dict[str, int]:
    """对全部 message 做 chunk 化并幂等入库;返回统计。语料顺序稳定。"""
    rows = conn.execute(
        "SELECT m.id AS message_id, m.conversation_id, m.content, m.seq AS message_seq,"
        " c.created_at AS conv_created"
        " FROM message m JOIN conversation c ON c.id = m.conversation_id"
        " ORDER BY c.created_at, m.conversation_id, m.seq"
    ).fetchall()
    inserted = 0
    total = 0
    empty_messages = 0
    for row in rows:
        pieces = split_text(row["content"])
        if not pieces:
            empty_messages += 1
            continue
        for seq, text in enumerate(pieces):
            total += 1
            chunk_id = stable_id("chunk", row["conversation_id"], str(row["message_seq"]), str(seq))
            cursor = conn.execute(
                "INSERT OR IGNORE INTO chunk (id, conversation_id, message_id, seq, text,"
                " token_count, created_at)"
                " VALUES (?,?,?,?,?,?,strftime('%Y-%m-%dT%H:%M:%SZ','now'))",
                (chunk_id, row["conversation_id"], row["message_id"], seq, text,
                 estimate_tokens(text)),
            )
            inserted += cursor.rowcount
    return {
        "messages": len(rows),
        "messages_empty": empty_messages,
        "chunks_total": total,
        "chunks_inserted": inserted,
    }
