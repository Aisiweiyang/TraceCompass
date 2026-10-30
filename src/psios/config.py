"""配置加载与校验(architecture.md §8)。

规则: 缺失的键用默认值补齐;但任何非法值(权重和偏离 1、非正阈值)
直接抛 ConfigError 拒绝运行,不静默回退。
"""

from __future__ import annotations

import tomllib
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any

# 进入加权和的 9 个分量(obligation 只用于 interest_type 分类,不进评分)
WEIGHT_KEYS = (
    "frequency",
    "recency",
    "persistence",
    "depth",
    "return_rate",
    "followup_density",
    "project_conversion",
    "connectivity",
    "voluntary",
)

_WEIGHT_TOLERANCE = 0.01


class ConfigError(ValueError):
    """配置缺失或非法。"""


@dataclass(frozen=True)
class StorageConfig:
    db_path: str = "data/psios.db"


@dataclass(frozen=True)
class ModelsConfig:
    embedding: str = "none"
    llm: str = "none"


@dataclass(frozen=True)
class LifecycleConfig:
    temporary_max_span: float = 14.0
    temporary_max_depth: float = 0.4
    emerging_window: float = 60.0
    emerging_min_persistence: float = 0.3
    dormant_min_gap: float = 90.0
    dormant_min_persistence: float = 0.5
    declining_window: float = 90.0
    declining_ratio: float = 0.3
    stable_min_months: float = 3.0
    stable_min_active_ratio: float = 0.25
    reactivated_recent_days: float = 30.0
    recency_half_life_days: float = 30.0
    session_gap_hours: float = 2.0


@dataclass(frozen=True)
class TopicsConfig:
    min_cluster_size: int = 8
    assign_threshold: float = 0.45       # embedding 向量的归簇阈值
    tfidf_assign_threshold: float = 0.12  # TF-IDF 降级向量的归簇阈值(相似度分布不同)
    merge_threshold: float = 0.85
    hierarchy_threshold: float = 0.5


@dataclass(frozen=True)
class ConfidenceConfig:
    evidence_saturation: int = 10


@dataclass(frozen=True)
class PsiosConfig:
    storage: StorageConfig = StorageConfig()
    models: ModelsConfig = ModelsConfig()
    interest_weights: dict[str, float] | None = None
    lifecycle: LifecycleConfig = LifecycleConfig()
    topics: TopicsConfig = TopicsConfig()
    confidence: ConfidenceConfig = ConfidenceConfig()

    def snapshot(self) -> dict[str, Any]:
        """全量配置快照(存入 model_run.config_snapshot)。"""
        return asdict(self)


def _section(raw: dict[str, Any], name: str) -> dict[str, Any]:
    value = raw.get(name, {})
    if not isinstance(value, dict):
        raise ConfigError(f"[{name}] 必须是 TOML 表")
    return value


def _build_dataclass(cls: type, raw: dict[str, Any], section: str) -> Any:
    """用 raw 中的已知键覆盖默认值;未知键报错(拼写错误不该被吞掉)。"""
    known = {f.name for f in fields(cls)}
    unknown = set(raw) - known
    if unknown:
        raise ConfigError(f"[{section}] 含未知键: {sorted(unknown)}")
    kwargs = {}
    for f in fields(cls):
        if f.name in raw:
            value = raw[f.name]
            if not isinstance(value, (int, float, str)) or isinstance(value, bool):
                raise ConfigError(f"[{section}].{f.name} 类型非法: {value!r}")
            if isinstance(f.type, str) and "int" in f.type and not isinstance(value, int):
                raise ConfigError(f"[{section}].{f.name} 必须是整数: {value!r}")
            kwargs[f.name] = value
    return cls(**kwargs)


def _validate_positive(section: str, obj: Any, exclude: set[str]) -> None:
    for f in fields(obj):
        value = getattr(obj, f.name)
        if f.name in exclude:
            continue
        # TOML 的 -90 是 int、-90.0 是 float,两者都必须校验(bool 是 int 子类,排除)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value <= 0:
            raise ConfigError(f"[{section}].{f.name} 必须为正数, 当前 {value}")


def _validate_weights(weights: dict[str, float]) -> None:
    if weights is None:
        raise ConfigError("缺少 [interest_weights]")
    missing = set(WEIGHT_KEYS) - set(weights)
    if missing:
        raise ConfigError(f"[interest_weights] 缺少分量: {sorted(missing)}")
    extra = set(weights) - set(WEIGHT_KEYS)
    if extra:
        raise ConfigError(
            f"[interest_weights] 含未知分量 {sorted(extra)}"
            f" (obligation 不进加权评分, 不应有权重)"
        )
    for key, value in weights.items():
        if not 0.0 <= value <= 1.0:
            raise ConfigError(f"[interest_weights].{key} 必须在 [0,1], 当前 {value}")
    total = sum(weights.values())
    if abs(total - 1.0) > _WEIGHT_TOLERANCE:
        raise ConfigError(f"[interest_weights] 权重和必须 ≈ 1 (容差 0.01), 当前 {total:.4f}")


def load_config(path: Path) -> PsiosConfig:
    """加载并校验配置;返回的 PsiosConfig.interest_weights 保证非 None。"""
    if not path.is_file():
        raise ConfigError(f"配置文件不存在: {path}")
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"TOML 解析失败: {exc}") from exc

    weights_raw = _section(raw, "interest_weights")
    weights: dict[str, float] = {}
    for key, value in weights_raw.items():
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise ConfigError(f"[interest_weights].{key} 必须是数字")
        weights[key] = float(value)
    _validate_weights(weights)

    lifecycle = _build_dataclass(LifecycleConfig, _section(raw, "lifecycle"), "lifecycle")
    topics = _build_dataclass(TopicsConfig, _section(raw, "topics"), "topics")
    confidence = _build_dataclass(ConfidenceConfig, _section(raw, "confidence"), "confidence")
    _validate_positive("lifecycle", lifecycle, exclude=set())
    _validate_positive("topics", topics, exclude={"min_cluster_size"})
    _validate_positive("confidence", confidence, exclude=set())
    if topics.min_cluster_size < 2:
        raise ConfigError("[topics].min_cluster_size 必须 ≥ 2")
    for key in ("assign_threshold", "tfidf_assign_threshold", "merge_threshold",
                "hierarchy_threshold"):
        value = getattr(topics, key)
        if not 0.0 < value < 1.0:
            raise ConfigError(f"[topics].{key} 必须在 (0,1), 当前 {value}")

    storage = _build_dataclass(StorageConfig, _section(raw, "storage"), "storage")
    models = _build_dataclass(ModelsConfig, _section(raw, "models"), "models")
    for name in ("embedding", "llm"):
        if not str(getattr(models, name)).strip():
            raise ConfigError(f"[models].{name} 不能为空 (用 \"none\" 表示关闭)")

    return PsiosConfig(
        storage=storage,
        models=models,
        interest_weights=weights,
        lifecycle=lifecycle,
        topics=topics,
        confidence=confidence,
    )


def resolve_db_path(config_path: Path, config: PsiosConfig) -> Path:
    """db_path 相对路径基于配置文件所在目录解析。"""
    db = Path(config.storage.db_path)
    if not db.is_absolute():
        db = config_path.resolve().parent / db
    return db
