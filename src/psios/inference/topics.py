"""Topic Discovery stage(inference-design v1.3 §1.2,research-log ENTRY-005)。

纯 Python 确定性 leader clustering:
- 语料稳定序遍历,余弦 ≥ assign_threshold 归入并更新簇心,否则自立新簇;
- min_cluster_size 后置过滤(小簇整体降孤儿,不建 topic 行);
- 层级 = 簇心再聚类,仅当产生 ≥2 个父簇才建,父簇不挂 chunk;
- topic 身份 = 成员 chunk 集合签名 → 全量重跑幂等;本次未重现的旧 topic 置 retired;
- 命名 LLM 可选,失败/关闭一律降级关键词(label_source 记录来源)。

已知边界(research-log): 人工 edit_label 仅在簇签名不变时保留;增量导入使成员
变化时会产生新签名(新 topic + 旧 topic retired),真正的 assign 增量语义属 M3+。
"""

from __future__ import annotations

import json
import sqlite3

from psios.config import PsiosConfig
from psios.ids import stable_id
from psios.llm.embedding import EmbeddingClient, EmbeddingError
from psios.llm.naming import KeywordNaming, LlmNaming, TopicNaming
from psios.llm.tfidf import build_tfidf, top_terms
from psios.llm.vectors import cosine, l2_normalize, pack_vector
from psios.pipeline.stages.chunk import run_chunk_stage

_REPRESENTATIVES = 3
_KEYWORDS_PER_TOPIC = 6


def _load_chunks(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """语料稳定顺序 = (会话创建时间, 会话 id, 消息序, 块序) —— 确定性的根基。"""
    return conn.execute(
        "SELECT ch.id, ch.text, ch.message_id, m.ts AS message_ts"
        " FROM chunk ch"
        " JOIN message m ON m.id = ch.message_id"
        " JOIN conversation c ON c.id = ch.conversation_id"
        " ORDER BY c.created_at, c.id, m.seq, ch.seq"
    ).fetchall()


def _leader_cluster(vectors: list[list[float]], threshold: float) -> list[list[int]]:
    """增量簇心聚类;返回簇列表(成员为向量下标,按首现顺序)。"""
    clusters: list[list[int]] = []
    centroids: list[list[float]] = []
    for idx, vec in enumerate(vectors):
        best_cluster = -1
        best_sim = -1.0
        for ci, centroid in enumerate(centroids):
            sim = cosine(vec, centroid)
            if sim > best_sim:
                best_sim = sim
                best_cluster = ci
        if best_cluster >= 0 and best_sim >= threshold:
            members = clusters[best_cluster]
            members.append(idx)
            dim = len(vec)
            mean = [0.0] * dim
            for mi in members:
                mv = vectors[mi]
                for d in range(dim):
                    mean[d] += mv[d]
            centroids[best_cluster] = l2_normalize([x / len(members) for x in mean])
        else:
            clusters.append([idx])
            centroids.append(list(vec))
    return clusters


def _centroids(vectors: list[list[float]], clusters: list[list[int]]) -> list[list[float]]:
    result: list[list[float]] = []
    for members in clusters:
        dim = len(vectors[members[0]])
        mean = [0.0] * dim
        for mi in members:
            mv = vectors[mi]
            for d in range(dim):
                mean[d] += mv[d]
        result.append(l2_normalize([x / len(members) for x in mean]))
    return result


def _assign_parents(centroids: list[list[float]], threshold: float) -> list[int | None]:
    """对簇心再聚类;返回每个叶簇的父簇序号。父簇 <2 个时全部置 None。"""
    parent_clusters = _leader_cluster(centroids, threshold)
    if len(parent_clusters) < 2:
        return [None] * len(centroids)
    leaf_to_parent: dict[int, int] = {}
    for pi, members in enumerate(parent_clusters):
        for leaf in members:
            leaf_to_parent[leaf] = pi
    return [leaf_to_parent.get(i) for i in range(len(centroids))]


def _time_range(rows: list[sqlite3.Row]) -> tuple[str | None, str | None]:
    stamps = sorted(row["message_ts"] for row in rows if row["message_ts"] is not None)
    return (stamps[0] if stamps else None, stamps[-1] if stamps else None)


def _revision(
    conn: sqlite3.Connection, entity_id: str, previous: dict, new: dict,
    reason: str, run_id: str, now: str,
) -> None:
    # 序号 = 该实体已有 revision 数 → 事件级唯一且确定(同输入重跑不碰撞)
    seq = conn.execute(
        "SELECT COUNT(*) FROM entity_revision WHERE entity_id = ?", (entity_id,)
    ).fetchone()[0]
    conn.execute(
        "INSERT INTO entity_revision (id, entity_type, entity_id, previous_state,"
        " new_state, reason, run_id, created_at)"
        " VALUES (?, 'topic', ?, ?, ?, ?, ?, ?)",
        (stable_id("revision", entity_id, now, reason, str(seq)),
         entity_id,
         json.dumps(previous, ensure_ascii=False),
         json.dumps(new, ensure_ascii=False),
         reason, run_id, now),
    )


def _upsert_topic(
    conn: sqlite3.Connection,
    topic_id: str,
    naming: TopicNaming,
    label_source: str,
    keywords: list[str],
    parent_id: str | None,
    confidence: float,
    method: str,
    model_version: str,
    first_seen: str | None,
    last_seen: str | None,
    run_id: str,
    now: str,
) -> str:
    """插入或更新 topic;实质变化写 entity_revision。返回 'new'|'kept'。"""
    row = conn.execute(
        "SELECT label, label_source, status FROM topic WHERE id = ?", (topic_id,)
    ).fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO topic (id, label, label_source, summary, keywords, parent_id,"
            " confidence, inference_method, model_version, status, first_seen, last_seen,"
            " run_id, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,'active',?,?,?,?,?)",
            (topic_id, naming.label, label_source, naming.summary,
             json.dumps(keywords, ensure_ascii=False), parent_id, confidence,
             method, model_version, first_seen, last_seen, run_id, now, now),
        )
        return "new"

    # 已存在: 复活为 active;人工标签不被覆盖;实质变化写 revision
    keep_human_label = row["label_source"] == "human"
    new_label = row["label"] if keep_human_label else naming.label
    new_label_source = "human" if keep_human_label else label_source
    changes: list[str] = []
    if new_label != row["label"]:
        changes.append(f"label:{row['label']!r}->{new_label!r}")
    if row["status"] != "active":
        changes.append(f"status:{row['status']}->active")
    if changes:
        _revision(conn, topic_id,
                  {"label": row["label"], "status": row["status"]},
                  {"label": new_label, "status": "active"},
                  "recompute:" + ";".join(changes), run_id, now)
    conn.execute(
        "UPDATE topic SET label = ?, label_source = ?, summary = ?, keywords = ?,"
        " parent_id = ?, confidence = ?, inference_method = ?, model_version = ?,"
        " status = 'active', first_seen = COALESCE(?, first_seen),"
        " last_seen = COALESCE(?, last_seen), run_id = ?, updated_at = ? WHERE id = ?",
        (new_label, new_label_source, naming.summary,
         json.dumps(keywords, ensure_ascii=False), parent_id, confidence,
         method, model_version, first_seen, last_seen, run_id, now, topic_id),
    )
    return "kept"


def _name_cluster(
    naming_client: LlmNaming | None,
    keyword_naming: KeywordNaming,
    representative_texts: list[str],
    keywords: list[str],
) -> tuple[TopicNaming, str] | None:
    """LLM 优先,失败/关闭降级关键词。返回 (命名, label_source) 或 None。"""
    if naming_client is not None:
        naming = naming_client.propose(representative_texts, keywords)
        if naming is not None:
            return naming, "llm"
    naming = keyword_naming.propose(representative_texts, keywords)
    return (naming, "keyword") if naming is not None else None


def run_topics_stage(
    conn: sqlite3.Connection,
    config: PsiosConfig,
    embedding_client: EmbeddingClient,
    naming_client: LlmNaming | None,
    run_id: str,
    now: str,
) -> dict[str, object]:
    """chunk → 向量 → 聚类 → 层级 → 命名 → 落库。幂等;返回统计。"""
    chunk_stats = run_chunk_stage(conn)
    stats: dict[str, object] = {"chunks": chunk_stats}
    rows = _load_chunks(conn)
    if not rows:
        stats["clusters_kept"] = 0
        stats["topics_new"] = 0
        stats["topics_retired"] = 0
        return stats

    texts = [row["text"] for row in rows]
    tfidf_vectors, vocab = build_tfidf(texts)   # 关键词通道,与 embedding 来源解耦

    try:
        api_vectors = embedding_client.embed(texts)
    except EmbeddingError as exc:  # API 中途失败 → 降级 TF-IDF,不拖垮 pipeline
        api_vectors = None
        stats["embedding_fallback"] = str(exc)
    if api_vectors is not None:
        cluster_vectors = [l2_normalize(v) for v in api_vectors]
        vector_model = embedding_client.model
    else:
        cluster_vectors = tfidf_vectors
        vector_model = "tfidf-char23"
    for row, vec in zip(rows, cluster_vectors, strict=True):
        conn.execute(
            "UPDATE chunk SET embedding = ?, embedding_model = ? WHERE id = ?",
            (pack_vector(vec), vector_model, row["id"]),
        )

    topics_cfg = config.topics
    # 阈值按向量类型选择: TF-IDF 与 embedding 的相似度分布不同(ENTRY-006)
    assign_threshold = (
        topics_cfg.assign_threshold
        if api_vectors is not None
        else topics_cfg.tfidf_assign_threshold
    )
    clusters = _leader_cluster(cluster_vectors, assign_threshold)
    kept = [c for c in clusters if len(c) >= topics_cfg.min_cluster_size]
    stats["clusters_raw"] = len(clusters)
    stats["clusters_kept"] = len(kept)
    stats["orphan_chunks"] = len(texts) - sum(len(c) for c in kept)

    kept_centroids = _centroids(cluster_vectors, kept)
    parent_of = _assign_parents(kept_centroids, topics_cfg.hierarchy_threshold)
    keyword_naming = KeywordNaming()

    leaf_topic_ids: list[str | None] = [None] * len(kept)
    current_topic_ids: set[str] = set()  # 本次运行中仍然活跃的 topic id
    new_topics = 0
    # (size, topic_id, label, label_source, is_parent) —— 元组排序保证确定性
    topics_meta: list[tuple[int, str, str, str, bool]] = []

    for ci, members in enumerate(kept):
        member_rows = [rows[mi] for mi in members]
        signature = stable_id("topic-signature", *[row["id"] for row in member_rows])
        topic_id = stable_id("topic", signature)
        current_topic_ids.add(topic_id)

        keywords = top_terms(tfidf_vectors, vocab, members, k=_KEYWORDS_PER_TOPIC)
        centroid = kept_centroids[ci]
        ranked = sorted(
            members, key=lambda mi: (-cosine(cluster_vectors[mi], centroid), rows[mi]["id"])
        )
        representative_rows = [rows[mi] for mi in ranked[:_REPRESENTATIVES]]
        named = _name_cluster(
            naming_client, keyword_naming,
            [row["text"] for row in representative_rows], keywords,
        )
        if named is None:
            continue
        naming, label_source = named

        evidence_count = len(representative_rows)
        confidence = 0.6 * min(1.0, evidence_count / config.confidence.evidence_saturation)
        first_seen, last_seen = _time_range(member_rows)
        result = _upsert_topic(
            conn, topic_id, naming, label_source, keywords, None, confidence,
            "leader_cluster", vector_model, first_seen, last_seen, run_id, now,
        )
        new_topics += result == "new"
        leaf_topic_ids[ci] = topic_id
        topics_meta.append((len(members), topic_id, naming.label, label_source, False))

        for row in member_rows:
            conn.execute(
                "INSERT OR IGNORE INTO topic_chunk (topic_id, chunk_id, weight, method, run_id)"
                " VALUES (?, ?, 1.0, 'leader', ?)",
                (topic_id, row["id"], run_id),
            )
        for row in representative_rows:
            conn.execute(
                "INSERT OR IGNORE INTO evidence (id, subject_type, subject_id, message_id,"
                " chunk_id, quote, signal_type, weight, run_id, created_at)"
                " VALUES (?, 'topic', ?, ?, ?, ?, 'cluster_member', 1.0, ?, ?)",
                (stable_id("evidence", "topic", topic_id, row["id"], "cluster_member"),
                 topic_id, row["message_id"], row["id"], row["text"][:200], run_id, now),
            )

    # ── 层级:仅当 ≥2 个父簇才建父 topic;父簇不挂 chunk ──────────────────
    parent_groups: dict[int, list[int]] = {}
    for leaf, parent in enumerate(parent_of):
        if parent is not None and leaf_topic_ids[leaf] is not None:
            parent_groups.setdefault(parent, []).append(leaf)

    if kept and len(parent_groups) >= 2:
        for _parent, leaves in sorted(parent_groups.items()):
            if len(leaves) < 2:
                continue  # 单叶"层级"无意义,且签名会与叶子相同(防自指, ENTRY-007)
            member_rows = [rows[mi] for leaf in leaves for mi in kept[leaf]]
            signature = stable_id(
                "topic-signature-parent", *[row["id"] for row in member_rows]
            )
            parent_id = stable_id("topic", signature)
            current_topic_ids.add(parent_id)

            merged_keywords: list[str] = []
            for leaf in sorted(leaves, key=lambda lf: -len(kept[lf])):
                for term in top_terms(tfidf_vectors, vocab, kept[leaf], k=3):
                    if term not in merged_keywords:
                        merged_keywords.append(term)
            keywords = merged_keywords[:_KEYWORDS_PER_TOPIC]
            # 代表文本 = 每个最大子簇的最近簇心各取 1 条
            representatives: list[str] = []
            for leaf in sorted(leaves, key=lambda lf: -len(kept[lf]))[:2]:
                centroid = kept_centroids[leaf]
                best = min(
                    kept[leaf],
                    key=lambda mi: (-cosine(cluster_vectors[mi], centroid), rows[mi]["id"]),
                )
                representatives.append(rows[best]["text"])
            named = _name_cluster(naming_client, keyword_naming, representatives, keywords)
            if named is None:
                continue
            naming, label_source = named
            _upsert_topic(
                conn, parent_id, naming, label_source, keywords, None, 0.4,
                "leader_cluster", "hierarchy", None, None, run_id, now,
            )
            topics_meta.append(
                (sum(len(kept[lf]) for lf in leaves), parent_id, naming.label, label_source, True)
            )
            # 回填子 topic 的 parent_id(实质变化留痕)
            for leaf in leaves:
                leaf_id = leaf_topic_ids[leaf]
                assert leaf_id is not None
                old = conn.execute(
                    "SELECT parent_id FROM topic WHERE id = ?", (leaf_id,)
                ).fetchone()
                if old is not None and old["parent_id"] != parent_id:
                    _revision(conn, leaf_id, {"parent_id": old["parent_id"]},
                              {"parent_id": parent_id}, "hierarchy_update", run_id, now)
                    conn.execute(
                        "UPDATE topic SET parent_id = ?, updated_at = ? WHERE id = ?",
                        (parent_id, now, leaf_id),
                    )

    # ── 本次未重现的旧 leader topic → retired(历史保留;kept 为空也要执行)──
    retired = 0
    for row in conn.execute(
        "SELECT id, status FROM topic WHERE inference_method = 'leader_cluster'"
    ).fetchall():
        if row["status"] == "active" and row["id"] not in current_topic_ids:
            conn.execute(
                "UPDATE topic SET status = 'retired', updated_at = ? WHERE id = ?",
                (now, row["id"]),
            )
            _revision(conn, row["id"], {"status": "active"}, {"status": "retired"},
                      "recompute_not_recreated", run_id, now)
            retired += 1

    stats["topics_new"] = new_topics
    stats["topics_retired"] = retired
    if not kept:
        stats["topic_list"] = []
        return stats
    stats["topic_list"] = [
        {"id": topic_id, "size": size, "label": label, "label_source": source,
         "parent": is_parent}
        for size, topic_id, label, source, is_parent in sorted(
            topics_meta, key=lambda item: (-item[0], item[1])
        )
    ]
    return stats
