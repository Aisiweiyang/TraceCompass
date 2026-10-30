"""Run an isolated synthetic demo using only the Python standard library."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from psios.cli import main as cli_main  # noqa: E402

ANCHORS = {
    "notes": (
        "Obsidian 双向链接笔记系统，个人知识管理工作流，读书笔记与知识整理。",
        "我的目标是在年底前完成读书笔记整理。",
        "我在开发ReadingGarden项目。",
        "如何整理读书笔记?",
    ),
    "hiking": (
        "周末徒步路线规划，山地步行装备，户外登山训练与路径记录。",
        "我计划坚持周末徒步训练。",
        "我在做TrailNotebook项目。",
        "如何选择徒步路线?",
    ),
    "cooking": (
        "家庭烹饪食谱整理，烘焙面包温度，厨房食材搭配与做饭笔记。",
        "我的目标是整理十二份家庭食谱。",
        "我在做RecipeNotebook项目。",
        "如何记录烘焙温度?",
    ),
}


def run_demo(directory: Path) -> int:
    # Refuse reuse, including empty directories: every invocation is isolated.
    directory.mkdir(parents=True, exist_ok=False)
    raw = directory / "raw"
    raw.mkdir()
    config = directory / "config.toml"
    config.write_bytes((ROOT / "config.toml").read_bytes())
    start = datetime(2026, 3, 1, 8, tzinfo=UTC)
    for name, texts in ANCHORS.items():
        rows = []
        for week in range(24):
            stamp = (start + timedelta(weeks=week)).isoformat()
            content = texts[0] + texts[week % len(texts)] + f"本周记录编号{week}。"
            rows.append(json.dumps(
                {"role": "user", "content": content, "ts": stamp}, ensure_ascii=False,
            ))
        (raw / f"synthetic_{name}.jsonl").write_text("\n".join(rows) + "\n", encoding="utf-8")
    manifest = {"synthetic": True, "messages": 72, "description": "Fictional demo only"}
    (directory / "SYNTHETIC.json").write_text(json.dumps(manifest), encoding="utf-8")
    common = ["--config", str(config)]
    for command in (
        ["init"], ["import", str(raw)], ["run", "topics"], ["run", "entities"],
        ["run", "interests"], ["invariants"], ["status"],
    ):
        result = cli_main([*command, *common])
        if result:
            return result
    print(f"\nSYNTHETIC demo: {directory}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serve", action="store_true", help="Open the local dashboard server")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--output", type=Path, help="A new, nonexistent demo directory")
    args = parser.parse_args()
    directory = args.output or ROOT / "data" / "demo" / (
        datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:8]
    )
    directory = directory.resolve()
    try:
        result = run_demo(directory)
    except FileExistsError:
        parser.error(f"Refusing to reuse existing path: {directory}")
    if result or not args.serve:
        return result
    return cli_main([
        "serve", "--config", str(directory / "config.toml"), "--port", str(args.port),
    ])


if __name__ == "__main__":
    raise SystemExit(main())
