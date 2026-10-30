"""配置加载与校验:非法配置必须拒绝,不静默回退。"""

from __future__ import annotations

from pathlib import Path

import pytest

from psios.config import ConfigError, load_config

VALID = """
[storage]
db_path = "data/x.db"

[interest_weights]
frequency = 0.15
recency = 0.10
persistence = 0.20
depth = 0.10
return_rate = 0.15
followup_density = 0.05
project_conversion = 0.10
connectivity = 0.05
voluntary = 0.10
"""


def _write(tmp_path: Path, content: str) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(content, encoding="utf-8")
    return path


def test_valid_config_loads(tmp_path: Path) -> None:
    config = load_config(_write(tmp_path, VALID))
    assert config.interest_weights is not None
    assert abs(sum(config.interest_weights.values()) - 1.0) <= 0.01
    assert config.lifecycle.dormant_min_gap == 90.0
    assert config.models.embedding == "none"
    snapshot = config.snapshot()
    assert snapshot["interest_weights"]["persistence"] == 0.20


def test_missing_config_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="不存在"):
        load_config(tmp_path / "nope.toml")


def test_broken_toml_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="TOML"):
        load_config(_write(tmp_path, "[interest_weights\nfrequency = 1"))


def test_weight_sum_violation_rejected(tmp_path: Path) -> None:
    bad = VALID.replace("persistence = 0.20", "persistence = 0.50")  # 总和 1.3
    with pytest.raises(ConfigError, match="权重和"):
        load_config(_write(tmp_path, bad))


def test_missing_weight_component_rejected(tmp_path: Path) -> None:
    bad = VALID.replace("connectivity = 0.05\n", "")
    with pytest.raises(ConfigError, match="缺少分量"):
        load_config(_write(tmp_path, bad))


def test_obligation_weight_rejected(tmp_path: Path) -> None:
    bad = VALID.replace("voluntary = 0.10", "voluntary = 0.10\nobligation = 0.0")
    with pytest.raises(ConfigError, match="obligation"):
        load_config(_write(tmp_path, bad))


def test_negative_threshold_rejected(tmp_path: Path) -> None:
    bad = VALID + "\n[lifecycle]\ndormant_min_gap = -90\n"
    with pytest.raises(ConfigError, match="正数"):
        load_config(_write(tmp_path, bad))


def test_unknown_key_rejected(tmp_path: Path) -> None:
    bad = VALID + "\n[lifecycle]\ndormant_min_gap_ = 90\n"
    with pytest.raises(ConfigError, match="未知键"):
        load_config(_write(tmp_path, bad))


def test_threshold_out_of_range_rejected(tmp_path: Path) -> None:
    bad = VALID + "\n[topics]\nassign_threshold = 1.5\n"
    with pytest.raises(ConfigError, match=r"\(0,1\)"):
        load_config(_write(tmp_path, bad))
