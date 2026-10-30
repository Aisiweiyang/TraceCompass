"""内容寻址稳定 ID(uuid5)。

更换命名空间 = 既有库全部 ID 失配,视为 schema 迁移级事件,禁止随意改动。
"""

from __future__ import annotations

import uuid

NS = uuid.uuid5(uuid.NAMESPACE_URL, "psios/v1/id-namespace")


def stable_id(*parts: str) -> str:
    return str(uuid.uuid5(NS, "|".join(parts)))
