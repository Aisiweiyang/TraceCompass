"""Inspect tracked and staged file content before publishing the repository."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BLOCKED_PARTS = {
    "data", "evaluation", "research_data", "output", "reports", ".venv", "venv",
    "__pycache__", "node_modules", ".pytest_cache", ".pytest_tmp", ".mypy_cache",
    ".ruff_cache", ".manual_build", ".build-tmp", ".release",
}
BLOCKED_SUFFIXES = {".db", ".sqlite", ".sqlite3", ".docx", ".pdf", ".pyc", ".log", ".pem", ".key"}
PATTERNS = {
    "GitHub credential": re.compile(
        r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})"
    ),
    "API credential": re.compile(r"\bsk-[A-Za-z0-9_-]{20,}"),
    "private key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "private Windows path": re.compile(
        r"[A-Za-z]:[\\/](?:Users|Documents and Settings)[\\/]", re.I
    ),
    "private POSIX path": re.compile(r"/(?:Users|home)/[A-Za-z0-9_.-]+/"),
}


def git_bytes(*args: str) -> bytes:
    return subprocess.check_output(["git", "-C", str(ROOT), *args])


def inspect(path: str, data: bytes) -> list[str]:
    parts = Path(path).parts
    name = parts[-1]
    errors = []
    if (set(parts) & BLOCKED_PARTS or Path(path).suffix.lower() in BLOCKED_SUFFIXES
            or name.startswith(".env") and name != ".env.example"
            or re.search(r"\.(?:db|sqlite3?)-", name)):
        errors.append(f"private/generated path: {path}")
    if len(data) > 1_000_000:
        errors.append(f"oversized file: {path}")
    try:
        content = data.decode("utf-8")
    except UnicodeDecodeError:
        errors.append(f"non-text file needs manual review: {path}")
        return errors
    for label, pattern in PATTERNS.items():
        if pattern.search(content):
            errors.append(f"{label}: {path}")  # Never print the credential itself.
    return errors


def main() -> int:
    paths = [p.decode("utf-8") for p in git_bytes("ls-files", "-z").split(b"\0") if p]
    errors: list[str] = []
    if not paths:
        errors.append("No tracked files; stage the intended public tree first.")
    for path in paths:
        entries = git_bytes("ls-files", "--stage", "--", path).decode("utf-8").splitlines()
        if len(entries) != 1 or not entries[0].startswith("100644 "):
            errors.append(f"non-regular or conflicted index entry: {path}")
            continue
        errors.extend(inspect(path, git_bytes("show", f":{path}")))
        disk = ROOT / path
        if disk.is_symlink():
            errors.append(f"symlink: {path}")
        elif disk.is_file():
            errors.extend(inspect(path, disk.read_bytes()))
    for error in sorted(set(errors)):
        print(error)
    if errors:
        return 1
    print(f"Public tree check passed: {len(paths)} text files; index and working tree inspected.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
