"""char 2–3 gram TF-IDF(纯 Python,确定性)。

用途有二:①`[models] embedding="none"` 时的降级向量;②所有主题的
关键词(top terms)来源 —— 与 embedding 通道解耦(inference-design §1.2a)。
"""

from __future__ import annotations

import math
import re
from collections import Counter

_KEEP = re.compile(r"[^0-9a-z\u4e00-\u9fff]+")
_NGRAMS = (2, 3)


def _normalize(text: str) -> str:
    return _KEEP.sub("", text.lower())


def char_ngrams(text: str) -> list[str]:
    normalized = _normalize(text)
    grams: list[str] = []
    for n in _NGRAMS:
        if len(normalized) >= n:
            grams.extend(normalized[i:i + n] for i in range(len(normalized) - n + 1))
    return grams


def build_tfidf(texts: list[str]) -> tuple[list[list[float]], list[str]]:
    """返回 (L2 归一化向量列表, 词表)。词表按字典序排列 → 同语料结果确定。

    语料为空或全无 n-gram 时返回空词表与对应零维向量(长度=文本数)。
    """
    doc_grams = [Counter(char_ngrams(t)) for t in texts]
    df: Counter[str] = Counter()
    for grams in doc_grams:
        df.update(grams.keys())
    vocab = sorted(df)
    index = {term: i for i, term in enumerate(vocab)}
    n_docs = len(texts)

    vectors: list[list[float]] = []
    for grams in doc_grams:
        vec = [0.0] * len(vocab)
        for term, count in grams.items():
            vec[index[term]] = count * (math.log((n_docs + 1) / (df[term] + 1)) + 1.0)
        _l2_normalize(vec)
        vectors.append(vec)
    return vectors, vocab


def top_terms(
    vectors: list[list[float]], vocab: list[str], member_idx: list[int], k: int = 6
) -> list[str]:
    """成员向量按维求和,取权重最高的 k 个词项(维度即词表项)。"""
    if not vocab or not member_idx:
        return []
    dim = len(vocab)
    summed = [0.0] * dim
    for idx in member_idx:
        vec = vectors[idx]
        for i in range(dim):
            summed[i] += vec[i]
    ranked = sorted(range(dim), key=lambda i: (-summed[i], vocab[i]))
    return [vocab[i] for i in ranked[:k] if summed[i] > 0.0]


def _l2_normalize(vec: list[float]) -> None:
    norm = math.sqrt(sum(x * x for x in vec))
    if norm > 0.0:
        for i, x in enumerate(vec):
            vec[i] = x / norm
