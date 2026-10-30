"""Ingestion:原始导出文件 → Conversation Schema → 幂等入库。

adapter 只做解析(不碰数据库);入库逻辑在 store.py。
"""

from psios.ingestion.adapter import (
    Adapter,
    AdapterError,
    ParseResult,
    detect_adapter,
    get_adapter,
)
from psios.ingestion.store import ImportStats, import_file, register_source

__all__ = [
    "Adapter",
    "AdapterError",
    "ImportStats",
    "ParseResult",
    "detect_adapter",
    "get_adapter",
    "import_file",
    "register_source",
]
