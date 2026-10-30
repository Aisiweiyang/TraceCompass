"""TraceCompass Dashboard (stdlib http.server, zero runtime dependencies).

原则: inspectability > 美观。所有动态内容经 html.escape;人工修正写
human_action + entity_revision(pipeline 尊重,可追溯)。
"""

from __future__ import annotations

import html
import json
import sqlite3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from psios.ids import stable_id

_STYLE = """
body{font-family:system-ui,'Segoe UI',sans-serif;margin:0;background:#f7f7f5;color:#222}
nav{background:#1f2937;padding:10px 16px}nav a{color:#e5e7eb;margin-right:14px;
text-decoration:none;font-size:14px}
main{max-width:960px;margin:20px auto;padding:0 12px}
h1{font-size:20px}h2{font-size:16px;margin-top:24px}
table{border-collapse:collapse;width:100%;background:#fff;font-size:14px}
th,td{border:1px solid #e5e7eb;padding:6px 8px;text-align:left}
th{background:#f3f4f6}tr:hover td{background:#f9fafb}
.score{font-weight:600;color:#065f46}
.muted{color:#9ca3af}.warn{color:#b45309;font-size:13px}
.quote{background:#fffbeb;border-left:3px solid #f59e0b;padding:4px 8px;margin:4px 0;
font-size:13px}
form{margin:8px 0}.btn{padding:3px 10px;font-size:13px}
.empty{color:#6b7280;font-style:italic;padding:16px;background:#fff;border:1px dashed #d1d5db}
"""


def page(title: str, body: str) -> str:
    nav_links = " ".join(
        f'<a href="{href}">{label}</a>'
        for href, label in [
            ("/", "概览"), ("/evolution", "演化"), ("/goals", "目标"),
            ("/projects", "项目"), ("/questions", "问题"),
            ("/meta-interests", "Meta"), ("/runs", "Runs"),
        ]
    )
    return (
        "<!doctype html><html lang=zh><head><meta charset=utf-8>"
        "<meta name=viewport content='width=device-width,initial-scale=1'>"
        f"<title>{html.escape(title)} — TraceCompass</title>"
        f"<style>{_STYLE}</style></head><body>"
        f"<nav>{nav_links}</nav><main>{body}</main></body></html>"
    )


def _esc(value: object) -> str:
    return html.escape(str(value if value is not None else "-"))


def _fmt_score(value: object) -> str:
    try:
        return f"{float(value):.3f}"  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return "-"


# ── 查询辅助 ────────────────────────────────────────────────────────────


def _coverage(conn: sqlite3.Connection) -> dict[str, Any]:
    row = conn.execute(
        "SELECT (SELECT COUNT(*) FROM conversation) AS conv,"
        " (SELECT COUNT(*) FROM message) AS msg,"
        " (SELECT COUNT(*) FROM chunk) AS chunk,"
        " (SELECT MIN(ts) FROM message WHERE ts IS NOT NULL) AS ts_min,"
        " (SELECT MAX(ts) FROM message WHERE ts IS NOT NULL) AS ts_max"
    ).fetchone()
    return dict(row)


def _visible_interests(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT i.topic_id, i.score, i.lifecycle, i.interest_type, i.confidence,"
        " v.label, v.topic_status FROM v_interest_current v"
        " JOIN interest i ON i.topic_id = v.topic_id"
        " ORDER BY i.score DESC, v.label"
    ).fetchall()


def _muted_count(conn: sqlite3.Connection) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM human_action h WHERE h.action_type = 'mute_interest'"
        " AND NOT EXISTS (SELECT 1 FROM human_action u WHERE"
        "  u.action_type = 'unmute_interest' AND u.target_id = h.target_id"
        "  AND u.created_at > h.created_at)"
    ).fetchone()[0]


def _interest_group(rows: list[sqlite3.Row], lifecycle: str | None) -> list[sqlite3.Row]:
    return [r for r in rows if r["lifecycle"] == lifecycle]


# ── 页面 ────────────────────────────────────────────────────────────────


def render_overview(conn: sqlite3.Connection) -> str:
    coverage = _coverage(conn)
    interests = _visible_interests(conn)
    muted = _muted_count(conn)
    counts = conn.execute(
        "SELECT (SELECT COUNT(*) FROM goal WHERE status NOT IN ('stale')) AS goals,"
        " (SELECT COUNT(*) FROM project) AS projects,"
        " (SELECT COUNT(*) FROM question WHERE status = 'recurring') AS questions_recurring,"
        " (SELECT COUNT(*) FROM question) AS questions_total,"
        " (SELECT COUNT(*) FROM meta_interest WHERE status = 'active') AS meta"
    ).fetchone()

    def group_table(title: str, rows: list[sqlite3.Row]) -> str:
        if not rows:
            return f"<h2>{_esc(title)}</h2><div class=empty>暂无</div>"
        body_rows = "".join(
            f"<tr><td><a href='/interest/{r['topic_id']}'>{_esc(r['label'])}</a></td>"
            f"<td class=score>{_fmt_score(r['score'])}</td>"
            f"<td>{_esc(r['interest_type'] or '-')}</td>"
            f"<td>{_fmt_score(r['confidence'])}</td></tr>"
            for r in rows
        )
        return (
            f"<h2>{_esc(title)}</h2><table><tr><th>主题</th><th>分数</th>"
            f"<th>类型</th><th>置信</th></tr>{body_rows}</table>"
        )

    parts = [
        "<h1>Personal Overview</h1>",
        f"<p>数据覆盖: {_coverage_str(coverage)}"
        + (f" <span class=warn>(已隐藏 {muted} 项兴趣)</span>" if muted else "")
        + "</p>",
        group_table("Top Interests", _interest_group(interests, None)
                    [:10] or interests[:10]),
        group_table("Emerging", _interest_group(interests, "emerging")),
        group_table("Stable", _interest_group(interests, "stable")),
        group_table("Dormant / Temporary", _interest_group(interests, "dormant")
                    + _interest_group(interests, "temporary")),
        "<h2>实体统计</h2><table><tr><th>类型</th><th>数量</th></tr>"
        f"<tr><td><a href=/goals>Goals</a></td><td>{counts['goals']}</td></tr>"
        f"<tr><td><a href=/projects>Projects</a></td><td>{counts['projects']}</td></tr>"
        f"<tr><td><a href=/questions>Questions</a></td>"
        f"<td>{counts['questions_recurring']} recurring / {counts['questions_total']}</td></tr>"
        f"<tr><td><a href=/meta-interests>Meta interests</a></td><td>{counts['meta']}</td></tr>"
        "</table>",
    ]
    return page("概览", "".join(parts))


def _coverage_str(coverage: dict[str, Any]) -> str:
    return (
        f"{coverage['conv']} 会话 / {coverage['msg']} 消息 / {coverage['chunk']} chunk"
        f" / 时间范围 {coverage['ts_min'] or '-'} ~ {coverage['ts_max'] or '-'}"
    )


def render_interest(conn: sqlite3.Connection, topic_id: str) -> str:
    topic = conn.execute(
        "SELECT id, label, summary, keywords, status, confidence, inference_method,"
        " model_version, first_seen, last_seen, parent_id FROM topic WHERE id = ?",
        (topic_id,),
    ).fetchone()
    if topic is None:
        return page("404", "<h1>未找到该主题</h1>")
    interest = conn.execute(
        "SELECT * FROM interest WHERE topic_id = ?", (topic_id,)
    ).fetchone()

    body: list[str] = [
        f"<h1>{_esc(topic['label'])}</h1>",
        f"<p>状态 {_esc(topic['status'])} · 方法 {_esc(topic['inference_method'])}"
        f" · 模型 {_esc(topic['model_version'])} · 置信 {_fmt_score(topic['confidence'])}"
        f" · {_esc(topic['first_seen'] or '-')} ~ {_esc(topic['last_seen'] or '-')}</p>",
    ]
    if topic["summary"]:
        body.append(f"<p>{_esc(topic['summary'])}</p>")
    keywords = json.loads(topic["keywords"] or "[]")
    if keywords:
        body.append("<p class=muted>关键词: " + _esc("、".join(keywords)) + "</p>")

    if interest is not None:
        scores = json.loads(interest["scores"] or "{}")
        raw = json.loads(interest["raw_stats"] or "{}")
        comp_rows = "".join(
            f"<tr><td>{_esc(name)}</td><td class=score>{_fmt_score(value)}</td></tr>"
            for name, value in scores.items()
        )
        body.append(
            f"<h2>评分(总分 {_fmt_score(interest['score'])},"
            f" 生命周期 {_esc(interest['lifecycle'] or '未分类')},"
            f" 类型 {_esc(interest['interest_type'] or '-')})</h2>"
            "<table><tr><th>分量</th><th>值</th></tr>" + comp_rows + "</table>"
            "<p class=muted>原始统计: " + _esc(json.dumps(raw, ensure_ascii=False)) + "</p>"
            "<h2>时间线(v_topic_weekly)</h2>" + _weekly_table(conn, topic_id)
        )
    else:
        body.append("<div class=empty>该主题尚无兴趣评分(运行 run interests)</div>")

    body.append("<h2>证据(为什么推断出这个主题)</h2>")
    evidence = conn.execute(
        "SELECT quote, signal_type, message_id FROM evidence WHERE subject_id = ?"
        " ORDER BY signal_type, created_at LIMIT 40",
        (topic_id,),
    ).fetchall()
    if evidence:
        body.append("".join(
            f"<div class=quote>«{_esc(e['quote'][:120])}»"
            f" <span class=muted>[{_esc(e['signal_type'])}]</span>"
            f" <a href='/message/{e['message_id']}'>原文</a></div>"
            for e in evidence
        ))
    else:
        body.append("<div class=empty>无证据记录</div>")

    revisions = conn.execute(
        "SELECT previous_state, new_state, reason, created_at FROM entity_revision"
        " WHERE entity_id = ? ORDER BY created_at DESC LIMIT 10",
        (topic_id,),
    ).fetchall()
    body.append("<h2>状态历史</h2>")
    if revisions:
        body.append("".join(
            f"<div class=quote>{_esc(r['reason'])} @ {_esc(r['created_at'])}:"
            f" {_esc(r['previous_state'])} → {_esc(r['new_state'])}</div>"
            for r in revisions
        ))
    else:
        body.append("<div class=empty>无变化记录</div>")

    body.append(
        f"<h2>人工修正</h2>"
        f"<form method=post action='/topics/{_esc(topic_id)}/label'>"
        f"<input name=label value='{_esc(topic['label'])}' size=40>"
        f" <button class=btn>改标签</button></form>"
        f"<form method=post action='/interests/{_esc(topic_id)}/mute'>"
        f"<button class=btn>隐藏该兴趣</button></form>"
        f"<form method=post action='/interests/{_esc(topic_id)}/unmute'>"
        f"<button class=btn>取消隐藏</button></form>"
        f"<form method=post action='/topics/merge'>"
        f"<input name=from value='{_esc(topic_id)}' type=hidden>"
        f" 合并到 topic id: <input name=to size=40>"
        f" <button class=btn>合并</button></form>"
    )
    return page(str(topic["label"]), "".join(body))


def _weekly_table(conn: sqlite3.Connection, topic_id: str) -> str:
    rows = conn.execute(
        "SELECT week, chunk_count, tokens FROM v_topic_weekly WHERE topic_id = ?"
        " ORDER BY week DESC LIMIT 24",
        (topic_id,),
    ).fetchall()
    if not rows:
        return "<div class=empty>无时间戳数据,无法生成时间线</div>"
    body_rows = "".join(
        f"<tr><td>{_esc(r['week'])}</td><td>{r['chunk_count']}</td>"
        f"<td>{r['tokens'] or 0}</td></tr>"
        for r in rows
    )
    return ("<table><tr><th>周</th><th>块数</th><th>token</th></tr>" + body_rows + "</table>")


def render_entity_list(
    conn: sqlite3.Connection, table: str, text_col: str, title: str
) -> str:
    kind_expr = "kind" if table == "goal" else "NULL"  # 仅 goal 有 kind 列
    rows = conn.execute(
        f"SELECT id, status, {kind_expr} AS kind, {text_col} AS text, first_seen"
        f" FROM {table} ORDER BY first_seen, id LIMIT 100"
    ).fetchall()
    if not rows:
        return page(title, f"<h1>{_esc(title)}</h1><div class=empty>暂无记录</div>")
    body_rows = "".join(
        f"<tr><td>{_esc(r['status'])}</td><td>{_esc(r['kind'] or '-')}</td>"
        f"<td>{_esc((r['text'] or '')[:80])}</td><td>{_esc(r['first_seen'] or '-')}</td>"
        f"<td><form method=post action='/{table}s/{r['id']}/confirm' "
        f"style='margin:0'><button class=btn>确认</button></form></td></tr>"
        for r in rows
    )
    return page(
        title,
        f"<h1>{_esc(title)}</h1><table><tr><th>状态</th><th>类别</th><th>内容</th>"
        f"<th>首见</th><th></th></tr>{body_rows}</table>",
    )


def render_runs(conn: sqlite3.Connection, run_id: str | None) -> str:
    if run_id:
        run = conn.execute(
            "SELECT * FROM model_run WHERE id = ?", (run_id,)
        ).fetchone()
        if run is None:
            return page("404", "<h1>未找到该 run</h1>")
        stats = json.dumps(json.loads(run["stats"] or "{}"), ensure_ascii=False, indent=1)
        stages = ", ".join(json.loads(run["stages"] or "[]"))
        return page(
            f"run {run_id[:8]}",
            f"<h1>run {run_id[:8]} [{_esc(run['status'])}]</h1>"
            f"<p>stages: {_esc(stages)}</p>"
            f"<p>开始 {_esc(run['started_at'])} / 结束 {_esc(run['finished_at'] or '-')}</p>"
            f"<pre style='background:#fff;padding:12px;border:1px solid #e5e7eb;"
            f"font-size:12px'>{_esc(stats)}</pre>",
        )
    rows = conn.execute(
        "SELECT id, status, stages, started_at, finished_at FROM model_run"
        " ORDER BY started_at DESC, rowid DESC LIMIT 30"
    ).fetchall()
    body_rows = "".join(
        f"<tr><td><a href='/runs/{r['id']}'>{r['id'][:10]}</a></td>"
        f"<td>{_esc(r['status'])}</td><td>{_esc(', '.join(json.loads(r['stages'] or '[]')))}</td>"
        f"<td>{_esc(r['started_at'])}</td><td>{_esc(r['finished_at'] or '-')}</td></tr>"
        for r in rows
    )
    return page(
        "Runs",
        "<h1>Pipeline 运行历史</h1><table><tr><th>id</th><th>状态</th><th>stages</th>"
        f"<th>开始</th><th>结束</th></tr>{body_rows}</table>",
    )


def render_evolution(conn: sqlite3.Connection) -> str:
    topics = conn.execute(
        "SELECT t.id, t.label, SUM(w.chunk_count) AS total FROM topic t"
        " JOIN v_topic_weekly w ON w.topic_id = t.id"
        " WHERE t.status = 'active' GROUP BY t.id ORDER BY total DESC LIMIT 10"
    ).fetchall()
    if not topics:
        return page("演化", "<h1>Topic Evolution</h1><div class=empty>无时间线数据</div>")
    weeks = [
        row["week"]
        for row in conn.execute(
            "SELECT DISTINCT week FROM v_topic_weekly ORDER BY week DESC LIMIT 16"
        )
    ]
    weeks.reverse()
    cells: dict[tuple[str, str], int] = {}
    for row in conn.execute(
        "SELECT topic_id, week, chunk_count FROM v_topic_weekly"
    ):
        cells[(row["topic_id"], row["week"])] = row["chunk_count"]
    max_count = max(cells.values(), default=1)
    header = "".join(f"<th>{_esc(w[-3:])}</th>" for w in weeks)
    body_rows = ""
    for topic in topics:
        row_cells = ""
        for week in weeks:
            count = cells.get((topic["id"], week), 0)
            intensity = min(220, 30 + 200 * count // max(1, max_count))
            row_cells += (
                f"<td style='background:rgb({255-intensity//3},{255-intensity},"
                f"{240-intensity})'>{count or ''}</td>"
            )
        body_rows += (
            f"<tr><td><a href='/interest/{topic['id']}'>{_esc(topic['label'][:24])}</a></td>"
            f"{row_cells}</tr>"
        )
    return page(
        "演化",
        "<h1>Topic Evolution</h1><table><tr><th>主题</th>" + header + "</tr>"
        + body_rows + "</table>",
    )


def render_message(conn: sqlite3.Connection, message_id: str) -> str:
    message = conn.execute(
        "SELECT m.content, m.role, m.ts, c.title FROM message m"
        " JOIN conversation c ON c.id = m.conversation_id WHERE m.id = ?",
        (message_id,),
    ).fetchone()
    if message is None:
        return page("404", "<h1>未找到该消息</h1>")
    topics = conn.execute(
        "SELECT DISTINCT t.label, t.id FROM topic_chunk tc"
        " JOIN chunk ch ON ch.id = tc.chunk_id JOIN topic t ON t.id = tc.topic_id"
        " WHERE ch.message_id = ? ORDER BY t.label",
        (message_id,),
    ).fetchall()
    topic_links = ", ".join(
        f"<a href='/interest/{row['id']}'>{_esc(row['label'])}</a>" for row in topics
    ) or "-"
    return page(
        "消息原文",
        f"<h1>消息原文 <span class=muted>({_esc(message['role'])},"
        f" {_esc(message['ts'] or '无时间')})</span></h1>"
        f"<p class=muted>会话: {_esc(message['title'] or '-')}</p>"
        f"<p class=muted>所属主题: {topic_links}</p>"
        f"<div style='background:#fff;padding:16px;border:1px solid #e5e7eb;white-space:"
        f"pre-wrap'>{_esc(message['content'])}</div>",
    )


# ── 人工修正 ────────────────────────────────────────────────────────────


def _human_action(
    conn: sqlite3.Connection, action: str, target_type: str, target_id: str,
    payload: dict, run_id: str | None, now: str,
) -> None:
    conn.execute(
        "INSERT INTO human_action (id, action_type, target_type, target_id, payload,"
        " created_at) VALUES (?,?,?,?,?,?)",
        (stable_id("human", action, target_id, now), action, target_type, target_id,
         json.dumps(payload, ensure_ascii=False), now),
    )
    _ = run_id  # 人工动作不在 pipeline run 内,run_id 留空


def apply_label(conn: sqlite3.Connection, topic_id: str, label: str, now: str) -> None:
    old = conn.execute("SELECT label FROM topic WHERE id = ?", (topic_id,)).fetchone()
    if old is None or not label.strip() or old["label"] == label:
        return
    seq = conn.execute(
        "SELECT COUNT(*) FROM entity_revision WHERE entity_id = ?", (topic_id,)
    ).fetchone()[0]
    conn.execute(
        "INSERT INTO entity_revision (id, entity_type, entity_id, previous_state,"
        " new_state, reason, created_at) VALUES (?, 'topic', ?, ?, ?, 'human_edit_label', ?)",
        (stable_id("revision", topic_id, now, "label", str(seq)), topic_id,
         json.dumps({"label": old["label"]}), json.dumps({"label": label}), now),
    )
    conn.execute(
        "UPDATE topic SET label = ?, label_source = 'human', updated_at = ? WHERE id = ?",
        (label, now, topic_id),
    )
    _human_action(conn, "edit_label", "topic", topic_id, {"label": label}, None, now)


def apply_merge(conn: sqlite3.Connection, from_id: str, to_id: str, now: str) -> bool:
    if from_id == to_id:
        return False
    exists = conn.execute(
        "SELECT COUNT(*) FROM topic WHERE id = ? AND status = 'active'", (to_id,)
    ).fetchone()[0]
    if not exists:
        return False
    conn.execute("UPDATE topic_chunk SET topic_id = ? WHERE topic_id = ?", (to_id, from_id))
    conn.execute(
        "UPDATE topic SET status = 'merged', merged_into_id = ?, updated_at = ?"
        " WHERE id = ?",
        (to_id, now, from_id),
    )
    seq = conn.execute(
        "SELECT COUNT(*) FROM entity_revision WHERE entity_id = ?", (from_id,)
    ).fetchone()[0]
    conn.execute(
        "INSERT INTO entity_revision (id, entity_type, entity_id, previous_state,"
        " new_state, reason, created_at) VALUES (?, 'topic', ?, ?, ?,"
        " 'human_merge', ?)",
        (stable_id("revision", from_id, now, "merge", str(seq)), from_id,
         json.dumps({"status": "active"}), json.dumps({"status": "merged",
                                                       "merged_into_id": to_id}), now),
    )
    _human_action(conn, "merge_topics", "topic", from_id, {"into": to_id}, None, now)
    return True


def apply_confirm(
    conn: sqlite3.Connection, table: str, entity_id: str, now: str
) -> bool:
    if table not in ("goal", "project", "question"):
        return False
    row = conn.execute(
        f"SELECT confidence FROM {table} WHERE id = ?", (entity_id,)
    ).fetchone()
    if row is None:
        return False
    new_conf = max(float(row["confidence"] or 0), 0.95)
    conn.execute(
        f"UPDATE {table} SET confidence = ?, updated_at = ? WHERE id = ?",
        (new_conf, now, entity_id),
    )
    _human_action(conn, "confirm_entity", table, entity_id,
                  {"confidence": new_conf}, None, now)
    return True


# ── HTTP 服务 ───────────────────────────────────────────────────────────


class PsiosHandler(BaseHTTPRequestHandler):
    db_path: str

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        pass  # 静默访问日志

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _html(self, content: str, status: int = 200) -> None:
        data = content.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _redirect(self, location: str) -> None:
        self.send_response(303)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path.rstrip("/") or "/"
        conn = self._conn()
        try:
            if path == "/":
                self._html(render_overview(conn))
            elif path == "/goals":
                self._html(render_entity_list(conn, "goal", "statement", "目标"))
            elif path == "/projects":
                self._html(render_entity_list(conn, "project", "name", "项目"))
            elif path == "/questions":
                self._html(render_entity_list(conn, "question", "display_text", "重复问题"))
            elif path == "/meta-interests":
                count = conn.execute(
                    "SELECT COUNT(*) FROM meta_interest WHERE status = 'active'"
                ).fetchone()[0]
                self._html(page(
                    "Meta interests",
                    "<h1>Meta Interests</h1>"
                    + ("<div class=empty>V0.1 尚未实现 Meta Interest 生成</div>"
                       if not count else f"<p>{count} 条(详情页后续版本提供)</p>"),
                ))
            elif path == "/runs":
                self._html(render_runs(conn, None))
            elif path == "/evolution":
                self._html(render_evolution(conn))
            elif path.startswith("/runs/"):
                self._html(render_runs(conn, path[len("/runs/"):]))
            elif path.startswith("/interest/"):
                self._html(render_interest(conn, path[len("/interest/"):]))
            elif path.startswith("/message/"):
                self._html(render_message(conn, path[len("/message/"):]))
            else:
                self._html(page("404", "<h1>404 Not Found</h1>"), 404)
        finally:
            conn.close()

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        form = parse_qs(self.rfile.read(length).decode("utf-8"))
        path = urlparse(self.path).path.rstrip("/")
        parts = path.strip("/").split("/")
        conn = self._conn()
        try:
            from datetime import UTC, datetime

            now = datetime.now(UTC).isoformat(timespec="seconds")
            if len(parts) == 3 and parts[0] == "topics" and parts[2] == "label":
                apply_label(conn, parts[1], form.get("label", [""])[0], now)
                conn.commit()
                self._redirect(f"/interest/{parts[1]}")
            elif len(parts) == 2 and parts[0] == "topics" and parts[1] == "merge":
                ok = apply_merge(conn, form.get("from", [""])[0],
                                 form.get("to", [""])[0], now)
                conn.commit()
                self._redirect(f"/interest/{form.get('to', [''])[0]}" if ok
                               else "/")
            elif len(parts) == 3 and parts[0] == "interests" \
                    and parts[2] in ("mute", "unmute"):
                _human_action(conn, f"{parts[2]}_interest", "topic", parts[1],
                              {}, None, now)
                conn.commit()
                self._redirect("/")
            elif len(parts) == 3 and parts[0] in ("goals", "projects", "questions") \
                    and parts[2] == "confirm":
                apply_confirm(conn, parts[0].rstrip("s"), parts[1], now)
                conn.commit()
                self._redirect(f"/{parts[0]}")
            else:
                self._html(page("404", "<h1>404 Not Found</h1>"), 404)
        finally:
            conn.close()


def make_server(db_path: str, port: int = 8000) -> ThreadingHTTPServer:
    handler = type("BoundHandler", (PsiosHandler,), {"db_path": db_path})
    return ThreadingHTTPServer(("127.0.0.1", port), handler)
