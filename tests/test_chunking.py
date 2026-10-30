"""chunk 化:预算、重叠、硬切、幂等、边界。"""

from __future__ import annotations

from psios.pipeline.stages.chunk import estimate_tokens, run_chunk_stage, split_text


def test_estimate_tokens_cjk_vs_ascii() -> None:
    assert estimate_tokens("面向对象") == 4
    assert estimate_tokens("abcd") == 1  # 4 ASCII 字符 ≈ 1 token
    assert estimate_tokens("") >= 1  # max(1, ...)


def test_split_short_text_single_chunk() -> None:
    assert split_text("一句话", max_tokens=500) == ["一句话"]


def test_split_empty_and_whitespace() -> None:
    assert split_text("") == []
    assert split_text("   \n  ") == []


def test_split_respects_budget_with_overlap() -> None:
    sentences = [f"这是第{i}句话,包含一些测试内容用来增加长度。" for i in range(40)]
    text = "".join(sentences)
    budget = 60
    pieces = split_text(text, max_tokens=budget)
    assert len(pieces) > 1
    for piece in pieces:
        assert estimate_tokens(piece) <= budget, f"块超预算: {estimate_tokens(piece)}"
    # 重叠: 每个后续块的开头应在上一个块中出现过(前块末句被复用)
    for prev, nxt in zip(pieces, pieces[1:], strict=False):
        assert nxt[:8] in prev


def test_split_hard_splits_oversized_single_sentence() -> None:
    text = "长" * 2000  # 无句边界的超长单句
    pieces = split_text(text, max_tokens=100)
    assert len(pieces) >= 20
    assert all(estimate_tokens(p) <= 100 for p in pieces)
    assert "".join(pieces).startswith("长长长")


def test_split_mixed_cjk_english_sentence_boundaries() -> None:
    text = (
        "Running a backtest with pandas needs care. 数据部分要清洗。"
        "Sharpe ratio matters. 最后一句没有句号"
    )
    pieces = split_text(text, max_tokens=15)
    assert len(pieces) > 1
    assert all(p.strip() for p in pieces)


def test_run_chunk_stage_idempotent(db) -> None:
    db.execute(
        "INSERT INTO model_run (id, config_snapshot, status, started_at)"
        " VALUES ('r1','{}','running','2026-09-13T00:00:00')"
    )
    db.execute("INSERT INTO source VALUES ('s1','n','jsonl','2026-09-13T00:00:00',NULL,NULL)")
    db.execute(
        "INSERT INTO conversation (id, source_id, external_id, title, started_at, ended_at,"
        " participants, content_hash, meta, created_at)"
        " VALUES ('c1','s1','e1','t','2026-09-13T00:00:00',NULL,'[]','h','{}',"
        "'2026-09-13T00:00:00')"
    )
    long_msg = "第一句话,用于切分测试。" * 80
    db.execute(
        "INSERT INTO message (id, conversation_id, seq, role, content, ts, content_hash,"
        " meta, created_at) VALUES ('m1','c1',0,'user',?,'2026-09-13T00:01:00','h','{}',"
        "'2026-09-13T00:00:00')",
        (long_msg,),
    )
    db.execute(
        "INSERT INTO message (id, conversation_id, seq, role, content, ts, content_hash,"
        " meta, created_at) VALUES ('m2','c1',1,'user','   ','2026-09-13T00:02:00','h2','{}',"
        "'2026-09-13T00:00:00')"
    )

    first = run_chunk_stage(db)
    assert first["messages"] == 2
    assert first["messages_empty"] == 1  # 纯空白消息跳过
    assert first["chunks_inserted"] == first["chunks_total"] > 1

    second = run_chunk_stage(db)
    assert second["chunks_inserted"] == 0  # 幂等
    assert second["chunks_total"] == first["chunks_total"]

    seqs = db.execute(
        "SELECT seq FROM chunk WHERE message_id = 'm1' ORDER BY seq"
    ).fetchall()
    assert [r[0] for r in seqs] == list(range(len(seqs)))  # 消息内连续序号
