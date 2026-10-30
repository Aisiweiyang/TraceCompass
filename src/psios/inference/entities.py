"""Goal / Project / Question 检测(inference-design §4–6,V0.1 纯规则路径)。

- 全确定性:正则候选 + 归一化聚类,无 LLM(inference_method='rule_regex');
- 实体身份 = 规范化文本签名 → 重跑幂等(INSERT OR IGNORE + 增量更新);
- recurring 判定:count ≥ 3 且跨 ≥ 2 个自然周;每次出现写 question_occurrence;
- 显式目标(kind=explicit)记 USER_STATED,推断目标记 INFERRED(spec 2.4);
- 证据一律回链原消息(不变量 #3)。
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime

from psios.ids import stable_id

# ── 文本工具 ────────────────────────────────────────────────────────────

# 句子切分:保留句尾标点(问句判定依赖句尾 ?)
_SENT_SPLIT = re.compile(r"[^。!!??\n;；]+[。!!??\n;；]?")
_NORMALIZE_KEEP = re.compile(r"[\s,。.!!??:：;;'\"“”‘’()()【】\[\]-]+")
QUESTION_START = re.compile(
    r"^(什么|怎么|为什么|哪个|哪些|哪|如何|是否|是不是|有没有|多久|多少|几|"
    r"how|what|why|which|when|where|should|can|could|is|are|do|does|did)\b",
    re.IGNORECASE,
)
GOAL_ZH = re.compile(r"(我想|我打算|我计划|我准备|我决定|我希望|我想要|我的目标是|目标是|我要)")
GOAL_EN = re.compile(
    r"\bI (want|plan|decided|decide|aim|intend) to\b|\bmy goal is\b|\bI'?m going to\b",
    re.IGNORECASE,
)
GOAL_EXPLICIT = re.compile(r"目标|OKR|goal\b|resolution", re.IGNORECASE)
PROJECT_ZH = re.compile(r"我在(做|开发|写|搞|搭|准备)([\u4e00-\u9fffA-Za-z0-9_\-\.]{2,24})")
PROJECT_ZH_SUFFIX = re.compile(r"([\u4e00-\u9fffA-Za-z0-9_\-\.]{2,24})项目")
PROJECT_EN = re.compile(
    r"\b(?:building|working on|developing)\s+([A-Za-z0-9_\-\.]{2,24})\b", re.IGNORECASE
)
_PROJECT_STOP = {
    "东西", "事情", "时候", "工作", "文章", "报告", "文档", "问题", "方案", "功能",
    "项目", "系统", "这个", "那个", "一个", "新", "小", "旧", "手头", "其他", "同样",
}


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENT_SPLIT.findall(text) if 4 <= len(s.strip()) <= 200]


def _normalize(text: str) -> str:
    return _NORMALIZE_KEEP.sub("", text).lower()


def _week_key(dt: datetime) -> str:
    iso = dt.isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"


def _parse_ts(value: str | None) -> datetime | None:
    """统一返回 naive UTC(模块内只做天数差等粗粒度比较)。"""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo:
        parsed = parsed.astimezone(UTC).replace(tzinfo=None)
    return parsed


@dataclass
class Candidate:
    normalized: str
    display: str          # 首次出现原文(句子)
    quote: str
    message_id: str
    ts: str | None
    kind: str             # goal: explicit|inferred


def _add_candidate(
    pool: dict[str, Candidate], normalized: str, cand: Candidate
) -> None:
    existing = pool.get(normalized)
    if existing is None:
        pool[normalized] = cand
    # 首见按时间序,天然保留最早 display


def _topics_for_message(conn: sqlite3.Connection, message_id: str) -> list[str]:
    rows = conn.execute(
        "SELECT DISTINCT tc.topic_id FROM topic_chunk tc"
        " JOIN chunk c ON c.id = tc.chunk_id WHERE c.message_id = ?"
        " ORDER BY tc.topic_id",
        (message_id,),
    ).fetchall()
    return [row[0] for row in rows]


def _write_evidence(
    conn: sqlite3.Connection, subject_type: str, subject_id: str,
    cand: Candidate, signal_type: str, run_id: str, now: str,
) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO evidence (id, subject_type, subject_id, message_id,"
        " chunk_id, quote, signal_type, weight, run_id, created_at)"
        " VALUES (?, ?, ?, ?, NULL, ?, ?, 1.0, ?, ?)",
        (stable_id("evidence", subject_type, subject_id, cand.message_id, signal_type),
         subject_type, subject_id, cand.message_id,
         cand.quote[:200], signal_type, run_id, now),
    )


def _revision(
    conn: sqlite3.Connection, entity_type: str, entity_id: str,
    previous: dict, new: dict, reason: str, run_id: str, now: str,
) -> None:
    seq = conn.execute(
        "SELECT COUNT(*) FROM entity_revision WHERE entity_id = ?", (entity_id,)
    ).fetchone()[0]
    conn.execute(
        "INSERT INTO entity_revision (id, entity_type, entity_id, previous_state,"
        " new_state, reason, run_id, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (stable_id("revision", entity_id, now, reason, str(seq)),
         entity_type, entity_id,
         json.dumps(previous, ensure_ascii=False),
         json.dumps(new, ensure_ascii=False), reason, run_id, now),
    )


# ── Goal ────────────────────────────────────────────────────────────────


def _goal_candidates(conn: sqlite3.Connection) -> dict[str, Candidate]:
    pool: dict[str, Candidate] = {}
    rows = conn.execute(
        "SELECT id, content, ts FROM message WHERE role = 'user' ORDER BY ts, id"
    ).fetchall()
    for row in rows:
        for sentence in _sentences(row["content"]):
            if GOAL_ZH.search(sentence) or GOAL_EN.search(sentence):
                normalized = _normalize(sentence)[:120]
                if not normalized:
                    continue
                kind = "explicit" if GOAL_EXPLICIT.search(sentence) else "inferred"
                _add_candidate(pool, normalized, Candidate(
                    normalized, sentence, sentence, row["id"], row["ts"], kind))
    return pool


def run_goal_detection(
    conn: sqlite3.Connection, run_id: str, now: str
) -> dict[str, int]:
    candidates = _goal_candidates(conn)
    inserted = updated = 0
    for normalized in sorted(candidates):
        cand = candidates[normalized]
        goal_id = stable_id("goal", normalized)
        row = conn.execute(
            'SELECT status, "count" FROM goal WHERE id = ?', (goal_id,)
        ).fetchone()
        topics = _topics_for_message(conn, cand.message_id)
        if row is None:
            conn.execute(
                "INSERT INTO goal (id, statement, kind, epistemic_status, status,"
                " confidence, inference_method, model_version, topic_ids, project_ids,"
                " first_seen, last_seen, run_id, created_at, updated_at)"
                " VALUES (?,?,?,?, 'proposed', ?, 'rule_regex', 'v0.1', ?, '[]',"
                " ?, ?, ?, ?, ?)",
                (goal_id, cand.display, cand.kind,
                 "USER_STATED" if cand.kind == "explicit" else "INFERRED",
                 0.7 * 0.9,
                 json.dumps(topics), cand.ts or now, cand.ts or now, run_id, now, now),
            )
            inserted += 1
        else:
            conn.execute(
                "UPDATE goal SET last_seen = MAX(COALESCE(last_seen,''), ?),"
                " run_id = ?, updated_at = ? WHERE id = ?",
                (cand.ts or now, run_id, now, goal_id),
            )
            updated += 1
        _write_evidence(conn, "goal", goal_id, cand, "goal_marker", run_id, now)

    # 目标 → 行动缺口基础: 长期无后续且无活跃 project → stale(留 revision)
    for row in conn.execute(
        "SELECT id, status, last_seen FROM goal WHERE status IN ('proposed','active')"
    ).fetchall():
        last = _parse_ts(row["last_seen"])
        if last is not None and (datetime.now() - last).days > 180:
            _revision(conn, "goal", row["id"], {"status": row["status"]},
                      {"status": "stale"}, "stale_no_followup", run_id, now)
            conn.execute("UPDATE goal SET status = 'stale', updated_at = ? WHERE id = ?",
                         (now, row["id"]))
    return {"goals_inserted": inserted, "goals_updated": updated}


# ── Project ─────────────────────────────────────────────────────────────


def _project_candidates(conn: sqlite3.Connection) -> dict[str, Candidate]:
    pool: dict[str, Candidate] = {}
    rows = conn.execute(
        "SELECT id, content, ts FROM message WHERE role = 'user' ORDER BY ts, id"
    ).fetchall()
    for row in rows:
        for sentence in _sentences(row["content"]):
            name: str | None = None
            match = PROJECT_ZH.search(sentence)
            if match:
                name = match.group(2)
            if name is None:
                match = PROJECT_EN.search(sentence)
                if match:
                    name = match.group(1)
            if name is None:
                match = PROJECT_ZH_SUFFIX.search(sentence)
                if match:
                    name = match.group(1)
            if not name or name in _PROJECT_STOP or len(name) < 2:
                continue
            normalized = _normalize(name)
            _add_candidate(pool, normalized, Candidate(
                normalized, name, sentence, row["id"], row["ts"], "inferred"))
    return pool


def run_project_detection(
    conn: sqlite3.Connection, run_id: str, now: str
) -> dict[str, int]:
    candidates = _project_candidates(conn)
    inserted = updated = 0
    for normalized in sorted(candidates):
        cand = candidates[normalized]
        project_id = stable_id("project", normalized)
        row = conn.execute(
            "SELECT status FROM project WHERE id = ?", (project_id,)
        ).fetchone()
        topics = _topics_for_message(conn, cand.message_id)
        if row is None:
            conn.execute(
                "INSERT INTO project (id, name, description, status, confidence,"
                " inference_method, model_version, topic_ids, goal_ids, first_seen,"
                " last_seen, run_id, created_at, updated_at)"
                " VALUES (?, ?, NULL, 'candidate', 0.6, 'rule_regex', 'v0.1', ?,"
                " '[]', ?, ?, ?, ?, ?)",
                (project_id, cand.display, json.dumps(topics),
                 cand.ts or now, cand.ts or now, run_id, now, now),
            )
            inserted += 1
        else:
            conn.execute(
                "UPDATE project SET last_seen = MAX(COALESCE(last_seen,''), ?),"
                " run_id = ?, updated_at = ? WHERE id = ?",
                (cand.ts or now, run_id, now, project_id),
            )
            updated += 1
        _write_evidence(conn, "project", project_id, cand, "project_marker", run_id, now)
    return {"projects_inserted": inserted, "projects_updated": updated}


# ── Question ────────────────────────────────────────────────────────────


def _question_occurrences(conn: sqlite3.Connection) -> list[tuple[Candidate, str]]:
    """返回 [(候选, 周键)] 按时间序;规范化文本相同的合并到首见候选。"""
    out: list[tuple[Candidate, str]] = []
    rows = conn.execute(
        "SELECT id, content, ts FROM message WHERE role = 'user' ORDER BY ts, id"
    ).fetchall()
    for row in rows:
        for sentence in _sentences(row["content"]):
            if not (sentence.endswith(("?", "?")) or QUESTION_START.match(sentence)):
                continue
            normalized = _normalize(sentence)[:120]
            if len(normalized) < 4:
                continue
            ts = _parse_ts(row["ts"])
            week = _week_key(ts) if ts else "unknown"
            out.append((Candidate(
                normalized, sentence, sentence, row["id"], row["ts"], "inferred"
            ), week))
    return out


def _cluster_questions(
    occurrences: list[tuple[Candidate, str]], threshold: float = 0.82
) -> dict[str, list[tuple[Candidate, str]]]:
    """char-3gram 余弦聚类(贪心,规范化文本按字典序保序)。"""
    from psios.llm.tfidf import build_tfidf
    from psios.llm.vectors import cosine

    order = sorted(range(len(occurrences)), key=lambda i: occurrences[i][0].normalized)
    texts = [occurrences[i][0].normalized for i in order]
    vectors, _ = build_tfidf(texts)
    groups: dict[int, list[int]] = {}
    leaders: list[tuple[int, list[float]]] = []
    for pos, idx in enumerate(order):
        target = vectors[pos]
        assigned = -1
        best = 0.0
        for gi, (_, centroid) in enumerate(leaders):
            sim = cosine(target, centroid)
            if sim > best:
                best = sim
                assigned = gi
        if assigned >= 0 and best >= threshold:
            groups[assigned].append(idx)
        else:
            leaders.append((idx, target))
            groups[len(leaders) - 1] = [idx]
    return {
        occurrences[leader_idx][0].normalized: [occurrences[i] for i in members]
        for (leader_idx, _), members in zip(leaders, groups.values(), strict=True)
    }


def run_question_detection(
    conn: sqlite3.Connection, run_id: str, now: str
) -> dict[str, int]:
    occurrences = _question_occurrences(conn)
    clusters = _cluster_questions(occurrences)
    inserted = updated = recurring_now = 0
    for normalized in sorted(clusters):
        members = clusters[normalized]
        display = max((m[0].display for m in members), key=len)
        first = members[0]
        question_id = stable_id("question", normalized)
        weeks = sorted({week for _, week in members if week != "unknown"})
        count = len(members)
        status = "recurring" if count >= 3 and len(weeks) >= 2 else "open"

        row = conn.execute(
            'SELECT status, "count" FROM question WHERE id = ?', (question_id,)
        ).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO question (id, normalized_text, display_text, count,"
                " distinct_weeks, status, confidence, first_seen, last_seen, topic_ids,"
                " run_id, created_at, updated_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (question_id, normalized, display, count, len(weeks), status,
                 round(0.9 * min(1.0, count / 10), 3),
                 first[0].ts or now,
                 members[-1][0].ts or now,
                 json.dumps(_topics_for_message(conn, first[0].message_id)),
                 run_id, now, now),
            )
            inserted += 1
        else:
            if row["status"] != status:
                _revision(conn, "question", question_id,
                          {"status": row["status"], "count": row["count"]},
                          {"status": status, "count": count},
                          "recurring_threshold", run_id, now)
                recurring_now += 1
            conn.execute(
                "UPDATE question SET count = ?, distinct_weeks = ?, status = ?,"
                " display_text = ?, last_seen = ?, run_id = ?, updated_at = ?"
                " WHERE id = ?",
                (count, len(weeks), status, display,
                 members[-1][0].ts or now, run_id, now, question_id),
            )
            updated += 1
        for cand, _week in members:
            conn.execute(
                "INSERT OR IGNORE INTO question_occurrence (id, question_id, message_id,"
                " quote, ts, created_at) VALUES (?,?,?,?,?,?)",
                (stable_id("qocc", question_id, cand.message_id, cand.quote[:60]),
                 question_id, cand.message_id, cand.quote, cand.ts, now),
            )
            _write_evidence(conn, "question", question_id, cand,
                            "question_repeat", run_id, now)
    return {
        "questions_inserted": inserted,
        "questions_updated": updated,
        "questions_turned_recurring": recurring_now,
    }


def run_entities_stage(
    conn: sqlite3.Connection, run_id: str, now: str
) -> dict[str, int]:
    stats: dict[str, int] = {}
    stats.update(run_goal_detection(conn, run_id, now))
    stats.update(run_project_detection(conn, run_id, now))
    stats.update(run_question_detection(conn, run_id, now))
    return stats
