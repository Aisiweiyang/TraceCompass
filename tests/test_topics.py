"""topics 阶段:合成语料聚类、确定性、降级链路、退役、evidence 覆盖。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from psios.config import PsiosConfig, load_config
from psios.inference.topics import run_topics_stage
from psios.ingestion import import_file
from psios.ingestion.jsonl import JsonlAdapter
from psios.llm.embedding import NoneEmbedding
from psios.llm.naming import LlmNaming
from psios.pipeline import runs
from psios.store import invariants

NOW = "2026-09-13T12:00:00+00:00"

# 三个词汇强分离的主题 × 10 条(中英混合)。
# 构造方式: 每条 = 主题锚定短语(固定) + 轮换短语 + 短尾注 → 主题内共享占比高,
# 跨主题几乎无共享 bigram(经实测: 主题内 ≥0.3, 跨主题 ≤0.05)。
_ANCHORS = {
    "scope": "示波器测量电路信号波形。示波器探头补偿调节。",
    "pkm": "Obsidian 双向链接笔记系统。个人知识管理工作流。",
    "quant": "backtest quant strategy with python pandas. moving average crossover.",
}
_ROTATIONS = {
    "scope": ["电源纹波测量方法", "触发模式与电平设置", "信号完整性检查要点",
              "带宽限制开关用途", "接地环路干扰排查"],
    "pkm": ["标签体系设计方案", "卡片盒笔记法实践", "本地优先同步方案",
            "定期回顾与清理", "社区插件推荐清单"],
    "quant": ["sharpe ratio calculation", "position sizing rules",
              "transaction cost model", "data cleaning pipeline",
              "parameter optimization risk"],
}
THEMES: dict[str, list[str]] = {
    name: [
        f"{_ANCHORS[name]}{_ROTATIONS[name][i % 5]}——个人笔记{i}。"
        if name != "quant"
        else f"{_ANCHORS[name]} {_ROTATIONS[name][i % 5]} note {i}."
        for i in range(10)
    ]
    for name in _ANCHORS
}


@pytest.fixture()
def topic_config(tmp_path: Path) -> PsiosConfig:
    path = tmp_path / "config.toml"
    path.write_text(
        '[storage]\ndb_path = "x.db"\n\n'
        "[interest_weights]\n"
        "frequency = 0.15\nrecency = 0.10\npersistence = 0.20\ndepth = 0.10\n"
        "return_rate = 0.15\nfollowup_density = 0.05\nproject_conversion = 0.10\n"
        "connectivity = 0.05\nvoluntary = 0.10\n",
        encoding="utf-8",
    )
    return load_config(path)


@pytest.fixture()
def seeded_db(db, tmp_path: Path):
    """导入三个主题的语料(每文件一会话,每消息一块)。"""
    for name, texts in THEMES.items():
        path = tmp_path / f"{name}.jsonl"
        path.write_text(
            "\n".join(
                json.dumps({"role": "user", "content": text}, ensure_ascii=False)
                for text in texts
            ),
            encoding="utf-8",
        )
        import_file(db, path, JsonlAdapter(), NOW)
    return db


def _run_stage(db, config: PsiosConfig, naming_client=None):
    run_id = runs.start_run(db, ["topics"], config.snapshot())
    return run_topics_stage(db, config, NoneEmbedding(), naming_client, run_id, NOW)


def _topic_rows(db) -> list[Any]:
    return db.execute(
        "SELECT t.id, t.label, t.label_source, t.status, COUNT(tc.chunk_id) AS size"
        " FROM topic t LEFT JOIN topic_chunk tc ON tc.topic_id = t.id"
        " WHERE t.status != 'retired' GROUP BY t.id ORDER BY size DESC, t.id"
    ).fetchall()


def test_full_chain_default_config_separates_themes(seeded_db, topic_config) -> None:
    """默认离线配置(TF-IDF + 关键词命名)可分出 3 个主题。"""
    stats = _run_stage(seeded_db, topic_config)
    assert stats["clusters_kept"] == 3
    rows = _topic_rows(seeded_db)
    assert len(rows) == 3
    assert all(row["size"] == 10 for row in rows)  # 每主题 10 块全部分配
    assert all(row["label_source"] == "keyword" for row in rows)
    labels = {row["label"] for row in rows}
    # CJK 锚定 gram 重复度高,中文主题标签可读;英文主题标签为 gram 拼接(已知局限)
    assert any("示波" in label or "电路" in label for label in labels)
    assert any("笔记" in label or "Obsidian" in label or "双向" in label
               for label in labels)
    assert len(labels) == 3  # 三个主题标签互不相同
    # evidence: 每主题 top-3 代表块
    counts = seeded_db.execute(
        "SELECT subject_id, COUNT(*) FROM evidence WHERE signal_type = 'cluster_member'"
        " GROUP BY subject_id"
    ).fetchall()
    assert len(counts) == 3
    assert all(row[1] == 3 for row in counts)
    invariants.assert_no_violations(seeded_db)


def test_rerun_is_idempotent(seeded_db, topic_config) -> None:
    """同一数据重跑:零新建、零退役、行集与标签逐字一致(DoD 确定性验收)。"""
    first = _run_stage(seeded_db, topic_config)
    rows_first = _topic_rows(seeded_db)
    second = _run_stage(seeded_db, topic_config)
    rows_second = _topic_rows(seeded_db)
    assert second["topics_new"] == 0
    assert second["topics_retired"] == 0
    assert [(r["id"], r["label"], r["size"]) for r in rows_first] == \
        [(r["id"], r["label"], r["size"]) for r in rows_second]
    assert first["clusters_kept"] == second["clusters_kept"]


def test_member_change_produces_new_signature_and_retires_old(
    seeded_db, topic_config
) -> None:
    """簇成员变化 → 新签名新 topic;旧 topic 置 retired 且保留 revision。"""
    _run_stage(seeded_db, topic_config)
    old_ids = {row["id"] for row in _topic_rows(seeded_db)}
    assert len(old_ids) == 3

    # 抬高门槛: 10 块的簇全部 < 11 → 孤儿化 → 旧 topic 全部退役
    strict_cfg = topic_config
    from dataclasses import replace

    strict_cfg = replace(strict_cfg, topics=replace(strict_cfg.topics, min_cluster_size=11))
    stats = _run_stage(seeded_db, strict_cfg)
    assert stats["clusters_kept"] == 0
    assert stats["topics_retired"] == 3
    assert _topic_rows(seeded_db) == []  # 无 active/merged topic
    statuses = seeded_db.execute("SELECT DISTINCT status FROM topic").fetchall()
    assert {row[0] for row in statuses} == {"retired"}
    revisions = seeded_db.execute(
        "SELECT COUNT(*) FROM entity_revision WHERE reason = 'recompute_not_recreated'"
    ).fetchone()[0]
    assert revisions == 3
    # 旧 id 集合仍可追溯(历史不删)
    assert all(
        seeded_db.execute("SELECT status FROM topic WHERE id = ?", (tid,)).fetchone()[0]
        == "retired"
        for tid in old_ids
    )


def test_llm_naming_used_when_available(seeded_db, topic_config) -> None:
    def transport(url: str, payload: dict[str, object], headers: dict[str, str]) -> dict:
        content = json.dumps(
            {"label": "统一命名", "summary": "s", "keywords": ["k"], "confidence": 0.9},
            ensure_ascii=False,
        )
        return {"choices": [{"message": {"content": content}}]}

    naming = LlmNaming("m", base_url="http://t", transport=transport)
    stats = _run_stage(seeded_db, topic_config, naming_client=naming)
    assert stats["clusters_kept"] == 3
    rows = _topic_rows(seeded_db)
    assert len(rows) == 3
    assert all(row["label"] == "统一命名" for row in rows)
    assert all(row["label_source"] == "llm" for row in rows)


def test_llm_failure_falls_back_to_keyword(seeded_db, topic_config) -> None:
    def transport(url: str, payload: dict[str, object], headers: dict[str, str]) -> dict:
        raise OSError("llm down")

    naming = LlmNaming("m", base_url="http://t", transport=transport)
    stats = _run_stage(seeded_db, topic_config, naming_client=naming)
    assert stats["clusters_kept"] == 3  # 命名失败不影响聚类
    assert all(row["label_source"] == "keyword" for row in _topic_rows(seeded_db))


def test_empty_database_no_crash(db, topic_config) -> None:
    stats = _run_stage(db, topic_config)
    assert stats["clusters_kept"] == 0
    assert stats["topics_new"] == 0
    invariants.assert_no_violations(db)


def test_orphans_not_assigned(seeded_db, topic_config) -> None:
    """min_cluster_size 过滤: 不到门槛的簇不建行。"""
    from dataclasses import replace

    strict = replace(topic_config, topics=replace(topic_config.topics, min_cluster_size=25))
    stats = _run_stage(seeded_db, strict)
    assert stats["clusters_kept"] == 0
    assert stats["orphan_chunks"] == 30
    assert seeded_db.execute("SELECT COUNT(*) FROM topic_chunk").fetchone()[0] == 0


def test_no_parent_topics_for_separated_themes(seeded_db, topic_config) -> None:
    """三个互相分离的主题: 父簇各只包 1 叶 → 不建父 topic, 无自指 parent_id。

    回归: 单叶父簇与叶子成员集合相同 → 签名相同 → topic 自己 parent 自己
    (research-log ENTRY-007)。
    """
    _run_stage(seeded_db, topic_config)
    rows = seeded_db.execute(
        "SELECT id, parent_id, model_version FROM topic"
    ).fetchall()
    assert len(rows) == 3
    for row in rows:
        assert row["parent_id"] is None, f"自指/多余父链接: {row['id']}"
        assert row["model_version"] == "tfidf-char23"  # 全部是叶 topic
    invariants.assert_no_violations(seeded_db)


def test_embedding_blob_written(seeded_db, topic_config) -> None:
    _run_stage(seeded_db, topic_config)
    row = seeded_db.execute(
        "SELECT embedding, embedding_model FROM chunk WHERE embedding IS NOT NULL LIMIT 1"
    ).fetchone()
    assert row is not None
    assert row["embedding_model"] == "tfidf-char23"
    assert len(row["embedding"]) > 4
