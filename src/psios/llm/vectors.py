"""向量基础运算:归一化 / 余弦 / float32 BLOB 编解码。纯 Python,零依赖。

BLOB 布局: 4 字节小端 uint32 维度前缀 + float32 小端值序列。
"""

from __future__ import annotations

import math
import struct

_LEN = struct.Struct("<I")


def l2_normalize(vec: list[float]) -> list[float]:
    """返回 L2 归一化副本;零向量原样返回。"""
    norm = math.sqrt(sum(x * x for x in vec))
    if norm <= 0.0:
        return list(vec)
    return [x / norm for x in vec]


def cosine(a: list[float], b: list[float]) -> float:
    """余弦相似度;任一为零向量返回 0。"""
    dot = 0.0
    na = 0.0
    nb = 0.0
    for x, y in zip(a, b, strict=False):
        dot += x * y
        na += x * x
        nb += y * y
    if na <= 0.0 or nb <= 0.0:
        return 0.0
    return dot / math.sqrt(na * nb)


def pack_vector(vec: list[float]) -> bytes:
    return _LEN.pack(len(vec)) + struct.pack(f"<{len(vec)}f", *vec)


def unpack_vector(blob: bytes | memoryview | None) -> list[float]:
    if not blob:
        return []
    data = bytes(blob)
    if len(data) < _LEN.size:
        return []
    (n,) = _LEN.unpack_from(data, 0)
    return list(struct.unpack_from(f"<{n}f", data, _LEN.size))
