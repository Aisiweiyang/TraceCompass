"""TraceCompass CLI: import, inference, inspection and local dashboard."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

from psios import __version__
from psios.config import ConfigError, PsiosConfig, load_config, resolve_db_path
from psios.inference.entities import run_entities_stage
from psios.inference.interests import run_interests_stage
from psios.inference.topics import run_topics_stage
from psios.ingestion import AdapterError, get_adapter, import_file
from psios.llm import make_embedding_client, make_naming_client
from psios.pipeline import runs
from psios.pipeline.assemble import build_snapshot
from psios.store import MigrationError, connect, current_version, invariants, migrate

DEFAULT_CONFIG = Path("config.toml")

_IMPORTABLE_SUFFIXES = {".json", ".jsonl", ".ndjson"}

# init/status/invariants 输出用;表顺序即建表顺序的镜像
_TABLES = [
    "model_run", "source", "conversation", "message", "chunk", "topic", "topic_chunk",
    "interest", "meta_interest", "goal", "project", "question", "question_occurrence",
    "evidence", "entity_revision", "personal_model_snapshot", "human_action",
]


def _open_db(db_override: Path | None, config_path: Path) -> tuple[PsiosConfig, Path]:
    """加载配置并解析 db 路径;--db 显式覆盖配置文件。"""
    config = load_config(config_path)
    db_path = db_override if db_override is not None else resolve_db_path(config_path, config)
    return config, db_path


def cmd_init(db_override: Path | None, config_path: Path) -> int:
    config, db_path = _open_db(db_override, config_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(db_path)
    try:
        applied = migrate(conn)
        invariants.assert_no_violations(conn)
        version = current_version(conn)
        tables = conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchone()[0]
        print(f"initialized: {db_path}")
        print(f"schema version: {version} (本次应用: {', '.join(applied) or '无, 已是最新'})")
        print(f"tables: {tables}")
        return 0
    finally:
        conn.close()


def cmd_status(db_override: Path | None, config_path: Path) -> int:
    _, db_path = _open_db(db_override, config_path)
    if not db_path.is_file():
        print(f"数据库不存在: {db_path}(先运行 psios init)")
        return 1
    conn = connect(db_path)
    try:
        print(f"db: {db_path}")
        print(f"schema version: {current_version(conn)}")
        for table in _TABLES:
            count = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]  # noqa: S608
            print(f"  {table:<24} {count}")
        for run in runs.latest_runs(conn, limit=5):
            stages = ", ".join(json.loads(run["stages"] or "[]"))
            print(f"run {run['id'][:8]} [{run['status']}] {run['started_at']} stages: {stages}")
        return 0
    finally:
        conn.close()


def cmd_invariants(db_override: Path | None, config_path: Path) -> int:
    _, db_path = _open_db(db_override, config_path)
    if not db_path.is_file():
        print(f"数据库不存在: {db_path}(先运行 psios init)")
        return 1
    conn = connect(db_path)
    try:
        results = invariants.run_all(conn)
        violations = {name: msgs for name, msgs in results.items() if msgs}
        if not violations:
            print("invariants: 全部通过")
            return 0
        for name, msgs in violations.items():
            for msg in msgs:
                print(f"[{name}] {msg}", file=sys.stderr)
        print(f"invariants: {sum(len(m) for m in violations.values())} 条违例", file=sys.stderr)
        return 1
    finally:
        conn.close()


def _iter_import_files(target: Path) -> list[Path]:
    """目标为文件 → 单文件;为目录 → 其下(不递归)全部可导入文件,按名排序。"""
    if target.is_file():
        return [target]
    return sorted(
        p for p in target.iterdir()
        if p.is_file() and p.suffix.lower() in _IMPORTABLE_SUFFIXES
    )


def cmd_import(
    path: Path,
    adapter_name: str,
    db_override: Path | None,
    config_path: Path,
) -> int:
    config, db_path = _open_db(db_override, config_path)
    if not db_path.is_file():
        print(f"数据库不存在: {db_path}(先运行 psios init)", file=sys.stderr)
        return 1
    target = path
    if not target.exists():
        print(f"路径不存在: {target}", file=sys.stderr)
        return 1
    files = _iter_import_files(target)
    if not files:
        print(f"没有可导入的文件(支持 {sorted(_IMPORTABLE_SUFFIXES)}): {target}", file=sys.stderr)
        return 1

    conn = connect(db_path)
    run_id = runs.start_run(conn, ["ingest"], config.snapshot())
    conn.commit()  # run 记录先落库:导入回滚时 run 仍可标记 failed
    try:
        now = datetime.now(UTC).isoformat(timespec="seconds")
        per_file: dict[str, dict[str, int]] = {}
        warnings: list[str] = []
        with conn:  # 单事务:任一文件失败则本次全部回滚
            for file_path in files:
                adapter = get_adapter(adapter_name, file_path)
                stats = import_file(conn, file_path, adapter, now)
                per_file[file_path.name] = stats.as_dict()
                warnings.extend(stats.warnings)
                counters = ", ".join(f"{k}={v}" for k, v in stats.as_dict().items())
                print(f"[{adapter.name}] {file_path.name}: {counters}")
        invariants.assert_no_violations(conn)
        runs.finish_run(conn, run_id, "done", {"files": per_file, "warnings": len(warnings)})
        conn.commit()
    except (AdapterError, sqlite3.Error, invariants.InvariantViolation) as exc:
        conn.rollback()
        runs.finish_run(conn, run_id, "failed", {"error": str(exc)})
        conn.commit()
        print(f"导入失败(已回滚): {exc}", file=sys.stderr)
        return 1
    finally:
        conn.close()

    for warning in warnings:
        print(f"warning: {warning}", file=sys.stderr)
    print(f"import done: run {run_id[:8]}, {len(files)} 个文件")
    return 0


def cmd_dump(
    what: str,
    conversation_id: str | None,
    limit: int,
    db_override: Path | None,
    config_path: Path,
) -> int:
    _, db_path = _open_db(db_override, config_path)
    if not db_path.is_file():
        print(f"数据库不存在: {db_path}(先运行 psios init)", file=sys.stderr)
        return 1
    conn = connect(db_path)
    try:
        if what == "conversations":
            rows = conn.execute(
                "SELECT c.id, c.external_id, c.title, c.started_at, COUNT(m.id) AS messages"
                " FROM conversation c LEFT JOIN message m ON m.conversation_id = c.id"
                " GROUP BY c.id ORDER BY c.created_at, c.id LIMIT ?",
                (limit,),
            ).fetchall()
            print(f"{'id':<40} {'ext_id':<26} {'msgs':>5}  title / started_at")
            for row in rows:
                title = (row["title"] or "")[:32]
                print(
                    f"{row['id'][:38]:<40} {row['external_id'][:24]:<26}"
                    f" {row['messages']:>5}  {title} / {row['started_at'] or '-'}"
                )
        elif what == "topics":
            rows = conn.execute(
                "SELECT t.id, t.label, t.label_source, t.status, t.parent_id,"
                " t.inference_method, t.model_version, t.first_seen, t.last_seen,"
                " COUNT(tc.chunk_id) AS size"
                " FROM topic t LEFT JOIN topic_chunk tc ON tc.topic_id = t.id"
                " GROUP BY t.id"
                " ORDER BY size DESC, t.id ASC LIMIT ?",
                (limit,),
            ).fetchall()
            print(
                f"{'id':<12} {'size':>5} {'status':<9} {'src':<8}"
                f" {'model':<14} label / first_seen"
            )
            for row in rows:
                label = (row["label"] or "")[:44]
                print(
                    f"{row['id'][:10]:<12} {row['size']:>5} {row['status']:<9}"
                    f" {row['label_source']:<8} {(row['model_version'] or '')[:12]:<14}"
                    f" {label} / {row['first_seen'] or '-'}"
                )
        elif what == "interests":
            rows = conn.execute(
                "SELECT i.topic_id, i.score, i.lifecycle, i.interest_type, i.confidence,"
                " t.label FROM interest i JOIN topic t ON t.id = i.topic_id"
                " ORDER BY i.score DESC, i.topic_id LIMIT ?",
                (limit,),
            ).fetchall()
            print(f"{'score':>6} {'lifecycle':<12} {'type':<13} {'conf':>5}  label")
            for row in rows:
                print(
                    f"{row['score']:>6.3f} {(row['lifecycle'] or '-'):<12}"
                    f" {(row['interest_type'] or '-'):<13} {row['confidence']:>5.2f}"
                    f"  {(row['label'] or '')[:40]}"
                )
        elif what in ("goals", "projects", "questions"):
            table = {"goals": "goal", "projects": "project", "questions": "question"}[what]
            text_col = {"goal": "statement", "project": "name",
                        "question": "display_text"}[table]
            rows = conn.execute(
                f"SELECT id, status, kind, first_seen, {text_col} AS text FROM {table}"
                " ORDER BY first_seen, id LIMIT ?" if table == "goal" else
                f"SELECT id, status, NULL AS kind, first_seen, {text_col} AS text"
                f" FROM {table} ORDER BY first_seen, id LIMIT ?",
                (limit,),
            ).fetchall()
            print(f"{'id':<12} {'status':<10} {'kind':<9} text")
            for row in rows:
                text = (row["text"] or "")[:60].replace("\n", " ")
                kind = (row["kind"] or "-")[:9]
                print(f"{row['id'][:10]:<12} {row['status']:<10} {kind:<9} {text}")
        elif what == "chunks":
            if not conversation_id:
                print("dump chunks 需要 --conversation <id>", file=sys.stderr)
                return 1
            rows = conn.execute(
                "SELECT ch.seq, ch.text, ch.token_count, m.seq AS message_seq"
                " FROM chunk ch JOIN message m ON m.id = ch.message_id"
                " WHERE ch.conversation_id = ?"
                " ORDER BY m.seq, ch.seq LIMIT ?",
                (conversation_id, limit),
            ).fetchall()
            if not rows:
                print("(该会话没有 chunk —— 先运行 run topics)")
                return 0
            print(f"{'msg':>4} {'chunk':>6} {'tok':>5} text")
            for row in rows:
                head = row["text"][:76].replace("\n", " ")
                print(
                    f"{row['message_seq']:>4} {row['seq']:>6} {row['token_count'] or 0:>5}  {head}"
                )
        else:
            params: list[object] = []
            where = ""
            if conversation_id is not None:
                where = " WHERE m.conversation_id = ?"
                params.append(conversation_id)
            params.append(limit)
            rows = conn.execute(
                "SELECT m.conversation_id, m.seq, m.role, m.ts, substr(m.content, 1, 72)"
                " AS head FROM message m" + where +
                " ORDER BY m.conversation_id, m.seq LIMIT ?",
                params,
            ).fetchall()
            print(f"{'conv':<12} {'seq':>4} {'role':<10} {'ts':<21} content")
            for row in rows:
                print(
                    f"{row['conversation_id'][:10]:<12} {row['seq']:>4} {row['role']:<10}"
                    f" {(row['ts'] or '-'):<21} {row['head'].replace(chr(10), ' ')}"
                )
        return 0
    finally:
        conn.close()


def cmd_run_topics(db_override: Path | None, config_path: Path) -> int:
    """M2: chunk → embed(TF-IDF 降级) → 聚类 → 命名。API 不可用自动降级。"""
    config, db_path = _open_db(db_override, config_path)
    if not db_path.is_file():
        print(f"数据库不存在: {db_path}(先运行 psios init)", file=sys.stderr)
        return 1
    embedding_client, warnings = make_embedding_client(config.models.embedding)
    naming_client, llm_warnings = make_naming_client(config.models.llm)
    warnings.extend(llm_warnings)

    conn = connect(db_path)
    run_id = runs.start_run(conn, ["chunk", "embed", "cluster", "name"], config.snapshot())
    conn.commit()
    try:
        now = datetime.now(UTC).isoformat(timespec="seconds")
        with conn:
            stats = run_topics_stage(conn, config, embedding_client, naming_client, run_id, now)
        invariants.assert_no_violations(conn)
        runs.finish_run(
            conn, run_id, "done",
            {k: v for k, v in stats.items() if k != "topic_list"},
        )
        conn.commit()
    except (sqlite3.Error, invariants.InvariantViolation) as exc:
        conn.rollback()
        runs.finish_run(conn, run_id, "failed", {"error": str(exc)})
        conn.commit()
        print(f"run topics 失败(已回滚): {exc}", file=sys.stderr)
        return 1
    finally:
        conn.close()

    for warning in warnings:
        print(f"warning: {warning}", file=sys.stderr)
    raw_topics = stats.get("topic_list")
    topics = (
        [t for t in raw_topics if isinstance(t, dict)] if isinstance(raw_topics, list) else []
    )
    if topics:
        print(f"{'id':<12} {'size':>5} {'src':<8} label")
        for topic in topics:
            print(
                f"{str(topic['id'])[:10]:<12} {int(topic['size']):>5} "
                f"{str(topic['label_source']):<8} {topic['label']}"
            )
    else:
        print("没有形成任何 topic(簇规模不足 min_cluster_size)")
    print(
        f"run topics done: run {run_id[:8]}, clusters={stats.get('clusters_kept', 0)},"
        f" orphans={stats.get('orphan_chunks', '-')},"
        f" retired={stats.get('topics_retired', 0)}"
    )
    return 0


def cmd_run_interests(db_override: Path | None, config_path: Path) -> int:
    """M3: 对 active topics 计算兴趣评分/生命周期,写 revision 与证据。"""
    config, db_path = _open_db(db_override, config_path)
    if not db_path.is_file():
        print(f"数据库不存在: {db_path}(先运行 psios init)", file=sys.stderr)
        return 1
    conn = connect(db_path)
    run_id = runs.start_run(conn, ["interest"], config.snapshot())
    conn.commit()
    try:
        now = datetime.now(UTC).isoformat(timespec="seconds")
        with conn:
            stats = run_interests_stage(conn, config, run_id, now)
            build_snapshot(conn, run_id, now)  # Personal Model 快照(DoD #12/#13)
        invariants.assert_no_violations(conn)
        runs.finish_run(conn, run_id, "done", {"actions": stats.get("actions", {})})
        conn.commit()
    except (sqlite3.Error, invariants.InvariantViolation) as exc:
        conn.rollback()
        runs.finish_run(conn, run_id, "failed", {"error": str(exc)})
        conn.commit()
        print(f"run interests 失败(已回滚): {exc}", file=sys.stderr)
        return 1
    finally:
        conn.close()

    raw_interests = stats.get("interests")
    interests = (
        [r for r in raw_interests if isinstance(r, dict)]
        if isinstance(raw_interests, list) else []
    )
    if interests:
        print(f"{'score':>6} {'lifecycle':<12} {'type':<13} {'conf':>5}  label")
        for row in interests:
            lifecycle = str(row["lifecycle"] or "-")
            print(
                f"{float(row['score']):>6.3f} {lifecycle:<12} {str(row['type']):<13}"
                f" {float(row['confidence']):>5.2f}  {str(row['label'])[:40]}"
            )
    else:
        print("没有可评分的 topic(先运行 run topics)")
    actions = stats.get("actions", {})
    print(f"run interests done: run {run_id[:8]}, actions={actions}")
    return 0


def cmd_run_entities(db_override: Path | None, config_path: Path) -> int:
    """M4: 规则检测 goal / project / question(含 recurring 判定)。"""
    config, db_path = _open_db(db_override, config_path)
    if not db_path.is_file():
        print(f"数据库不存在: {db_path}(先运行 psios init)", file=sys.stderr)
        return 1
    conn = connect(db_path)
    run_id = runs.start_run(conn, ["goal", "project", "question"], config.snapshot())
    conn.commit()
    try:
        now = datetime.now(UTC).isoformat(timespec="seconds")
        with conn:
            stats = run_entities_stage(conn, run_id, now)
        invariants.assert_no_violations(conn)
        runs.finish_run(conn, run_id, "done", stats)
        conn.commit()
    except (sqlite3.Error, invariants.InvariantViolation) as exc:
        conn.rollback()
        runs.finish_run(conn, run_id, "failed", {"error": str(exc)})
        conn.commit()
        print(f"run entities 失败(已回滚): {exc}", file=sys.stderr)
        return 1
    finally:
        conn.close()
    print(f"run entities done: run {run_id[:8]}, {stats}")
    return 0


def cmd_serve(port: int, db_override: Path | None, config_path: Path) -> int:
    """本地 Dashboard;阻塞运行,Ctrl-C 退出。"""
    _, db_path = _open_db(db_override, config_path)
    if not db_path.is_file():
        print(f"数据库不存在: {db_path}(先运行 psios init)", file=sys.stderr)
        return 1
    from psios.web import make_server

    server = make_server(str(db_path), port)
    print(f"TraceCompass Dashboard: http://127.0.0.1:{port}/  (Ctrl-C 停止)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        server.server_close()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="tracecompass",
        description=f"TraceCompass v{__version__} — 有证据的个人对话分析",
    )
    parser.add_argument("--version", action="version", version=f"TraceCompass {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    def add_common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--config", type=Path, default=DEFAULT_CONFIG, help="配置文件路径")
        p.add_argument("--db", type=Path, default=None, help="覆盖配置中的 db_path")

    add_common(sub.add_parser("init", help="创建数据目录并应用全部迁移"))
    add_common(sub.add_parser("status", help="查看 schema 版本、各表行数与最近 runs"))
    add_common(sub.add_parser("invariants", help="运行数据库不变量检查"))

    p_import = sub.add_parser("import", help="导入聊天导出文件(幂等,可重复运行)")
    p_import.add_argument("path", type=Path, help="导出文件或目录(如 data/raw)")
    p_import.add_argument(
        "--adapter", default="auto", choices=["auto", "chatgpt_json", "jsonl"]
    )
    add_common(p_import)

    p_dump = sub.add_parser("dump", help="查看已导入数据与中间产物")
    p_dump.add_argument(
        "what",
        choices=["conversations", "messages", "topics", "chunks", "interests",
                 "goals", "projects", "questions"],
    )
    p_dump.add_argument("--conversation", default=None, help="会话 ID(messages/chunks 过滤)")
    p_dump.add_argument("--limit", type=int, default=50)
    add_common(p_dump)

    p_run = sub.add_parser("run", help="运行 pipeline 阶段")
    p_run.add_argument(
        "stage", choices=["topics", "interests", "entities"],
        help="topics = chunk→聚类→命名; interests = 兴趣评分; entities = 目标/项目/问题",
    )
    add_common(p_run)

    p_serve = sub.add_parser("serve", help="启动本地 Dashboard(仅监听 127.0.0.1)")
    p_serve.add_argument("--port", type=int, default=8000)
    add_common(p_serve)

    args = parser.parse_args(argv)
    try:
        if args.command == "init":
            return cmd_init(args.db, args.config)
        if args.command == "status":
            return cmd_status(args.db, args.config)
        if args.command == "invariants":
            return cmd_invariants(args.db, args.config)
        if args.command == "import":
            return cmd_import(args.path, args.adapter, args.db, args.config)
        if args.command == "dump":
            return cmd_dump(args.what, args.conversation, args.limit, args.db, args.config)
        if args.command == "run":
            if args.stage == "topics":
                return cmd_run_topics(args.db, args.config)
            if args.stage == "interests":
                return cmd_run_interests(args.db, args.config)
            return cmd_run_entities(args.db, args.config)
        if args.command == "serve":
            return cmd_serve(args.port, args.db, args.config)
    except ConfigError as exc:
        print(f"配置错误: {exc}", file=sys.stderr)
        return 1
    except MigrationError as exc:
        print(f"迁移失败: {exc}", file=sys.stderr)
        return 1
    except invariants.InvariantViolation as exc:
        print(str(exc), file=sys.stderr)
        return 1
    parser.error(f"未知命令: {args.command}")
    return 2  # pragma: no cover — parser.error 必然退出


if __name__ == "__main__":
    raise SystemExit(main())
