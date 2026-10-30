"""LLM 通道:TF-IDF、向量编解码、embedding API(注入 transport)、命名降级。"""

from __future__ import annotations

import json

import pytest

from psios.llm import KeywordNaming, build_tfidf, top_terms
from psios.llm.embedding import ApiEmbedding, EmbeddingError, NoneEmbedding
from psios.llm.naming import LlmNaming, parse_naming
from psios.llm.vectors import cosine, l2_normalize, pack_vector, unpack_vector

# ── TF-IDF ─────────────────────────────────────────────────────────────


def test_tfidf_deterministic_and_normalized() -> None:
    texts = ["示波器测量电路信号", "oscilloscope probe circuit", "知识管理笔记系统"]
    v1, vocab1 = build_tfidf(texts)
    v2, vocab2 = build_tfidf(texts)
    assert vocab1 == vocab2 and vocab1 == sorted(vocab1)
    assert all(a == b for a, b in zip(v1, v2, strict=True))
    for vec in v1:
        assert abs(sum(x * x for x in vec) - 1.0) < 1e-6  # L2 归一化


def test_tfidf_theme_separation() -> None:
    theme_a = ["示波器测量电路信号波形", "示波器探头接地测量电路"]
    theme_b = ["知识管理笔记系统工作流", "知识库笔记系统标签管理"]
    vectors, _ = build_tfidf(theme_a + theme_b)
    assert cosine(vectors[0], vectors[1]) > cosine(vectors[0], vectors[2])


def test_tfidf_empty_corpus() -> None:
    vectors, vocab = build_tfidf([])
    assert vectors == [] and vocab == []


def test_top_terms_ranking() -> None:
    texts = ["示波器示波器示波器测量", "知识管理知识管理知识", "其他内容文本"]
    vectors, vocab = build_tfidf(texts)
    terms = top_terms(vectors, vocab, [0], k=3)
    assert terms and any("示波" in t or "波器" in t for t in terms)
    assert top_terms(vectors, vocab, [], k=3) == []


# ── 向量编解码 ──────────────────────────────────────────────────────────


def test_vector_pack_roundtrip() -> None:
    vec = [0.1, -0.25, 3.5, 0.0]
    blob = pack_vector(vec)
    restored = unpack_vector(blob)
    assert len(restored) == 4
    assert all(abs(a - b) < 1e-6 for a, b in zip(vec, restored, strict=True))
    assert unpack_vector(None) == []
    assert unpack_vector(b"") == []


def test_cosine_basics() -> None:
    assert abs(cosine([1, 0], [0, 1])) < 1e-12
    assert abs(cosine([1, 0], [1, 0]) - 1.0) < 1e-12
    assert cosine([0, 0], [1, 0]) == 0.0
    normalized = l2_normalize([3.0, 4.0])
    assert abs(sum(x * x for x in normalized) - 1.0) < 1e-12


# ── Embedding API(transport 注入,无真实网络)────────────────────────────


def _fake_transport(batches: list[list[list[float]]], fail_first: bool = False):
    calls = {"n": 0}

    def transport(url: str, payload: dict[str, object], headers: dict[str, str]) -> dict:
        calls["n"] += 1
        if fail_first and calls["n"] == 1:
            raise OSError("network down")
        texts = payload["input"]
        assert isinstance(texts, list)
        vectors = batches[calls["n"] - (2 if fail_first else 1)]
        assert len(texts) == len(vectors)  # 批量与响应对齐
        return {"data": [{"embedding": v, "index": i} for i, v in enumerate(vectors)]}

    return transport, calls


def test_api_embedding_batches_and_alignment() -> None:
    transport, calls = _fake_transport([[[1.0, 0.0], [0.0, 1.0]], [[0.5, 0.5]]])
    client = ApiEmbedding("m", base_url="http://t", api_key="k", batch_size=2,
                          transport=transport)
    vectors = client.embed(["a", "b", "c"])
    assert calls["n"] == 2
    assert vectors == [[1.0, 0.0], [0.0, 1.0], [0.5, 0.5]]


def test_api_embedding_retries_once() -> None:
    transport, calls = _fake_transport([[[1.0, 0.0]]], fail_first=True)
    client = ApiEmbedding("m", base_url="http://t", batch_size=4, transport=transport)
    assert client.embed(["a"]) == [[1.0, 0.0]]
    assert calls["n"] == 2  # 失败 1 次 + 重试成功


def test_api_embedding_fails_after_retry() -> None:
    def transport(url: str, payload: dict[str, object], headers: dict[str, str]) -> dict:
        raise OSError("still down")

    client = ApiEmbedding("m", base_url="http://t", transport=transport)
    with pytest.raises(EmbeddingError, match="已重试"):
        client.embed(["a"])


def test_api_embedding_rejects_misaligned_response() -> None:
    def transport(url: str, payload: dict[str, object], headers: dict[str, str]) -> dict:
        return {"data": [{"embedding": [1.0]}]}  # 1 条,期望 2 条

    client = ApiEmbedding("m", base_url="http://t", batch_size=4, transport=transport)
    with pytest.raises(EmbeddingError, match="错位"):
        client.embed(["a", "b"])


def test_none_embedding_returns_none() -> None:
    assert NoneEmbedding().embed(["a"]) is None


# ── 命名 ────────────────────────────────────────────────────────────────


def test_parse_naming_valid_and_fenced() -> None:
    naming = parse_naming(
        {"label": "电路调试", "summary": "s", "keywords": ["示波器"], "confidence": 0.8}
    )
    assert naming is not None and naming.label == "电路调试"
    fenced = parse_naming(
        json.loads('{"label": "x", "keywords": [], "confidence": 1.5}')) \
        if False else parse_naming({"label": "x", "keywords": [], "confidence": 1.5})
    assert fenced is not None and fenced.confidence == 1.0  # 越界截断


def test_parse_naming_rejects_invalid() -> None:
    assert parse_naming(None) is None
    assert parse_naming({"label": "", "keywords": [], "confidence": 0.5}) is None
    assert parse_naming({"label": "x", "keywords": "not-a-list", "confidence": 0.5}) is None
    assert parse_naming({"label": "x", "keywords": [], "confidence": "high"}) is None
    assert parse_naming({"keywords": [], "confidence": 0.5}) is None


def test_llm_naming_parses_content_and_fenced_json() -> None:
    def transport(url: str, payload: dict[str, object], headers: dict[str, str]) -> dict:
        content = (
            '```json\n{"label": "主题", "summary": null,'
            ' "keywords": ["k"], "confidence": 0.5}\n```'
        )
        return {"choices": [{"message": {"content": content}}]}

    naming = LlmNaming("m", base_url="http://t", transport=transport)
    result = naming.propose(["代表文本"], ["k"])
    assert result is not None and result.label == "主题" and result.summary is None


def test_llm_naming_degrades_on_bad_output() -> None:
    def transport(url: str, payload: dict[str, object], headers: dict[str, str]) -> dict:
        return {"choices": [{"message": {"content": "不是 JSON"}}]}

    naming = LlmNaming("m", base_url="http://t", transport=transport)
    assert naming.propose(["t"], ["k"]) is None


def test_llm_naming_degrades_on_transport_error() -> None:
    def transport(url: str, payload: dict[str, object], headers: dict[str, str]) -> dict:
        raise OSError("down")

    naming = LlmNaming("m", base_url="http://t", transport=transport)
    assert naming.propose(["t"], ["k"]) is None


def test_keyword_naming() -> None:
    naming = KeywordNaming()
    result = naming.propose(["t1", "t2"], ["示波器", "电路", "信号"])
    assert result is not None
    assert "示波器" in result.label
    assert result.confidence <= 0.6
    assert naming.propose(["t"], []) is None  # 无关键词 → None
