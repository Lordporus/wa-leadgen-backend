"""Fail CI only for Ruff or Mypy diagnostics introduced after a base commit."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
Diagnostic = tuple[str, str, str]
MYPY_ERROR = re.compile(
    r"^(?P<path>.+?):\d+(?::\d+)?: error: "
    r"(?P<message>.*?)(?:  \[(?P<code>[^\]]+)\])?$"
)
MYPY_FILE_ERROR = re.compile(
    r"^(?P<path>.+?): error: "
    r"(?P<message>.*?)(?:  \[(?P<code>[^\]]+)\])?$"
)


def _run(
    command: list[str],
    *,
    cwd: Path,
    allowed_codes: set[int],
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        command,
        cwd=cwd,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode not in allowed_codes:
        output = (result.stdout + result.stderr).strip()
        raise RuntimeError(
            f"Command failed with exit {result.returncode}: "
            f"{' '.join(command)}\n{output}"
        )
    return result


def _relative_path(raw_path: str, cwd: Path) -> str:
    path = Path(raw_path)
    if path.is_absolute():
        try:
            path = path.resolve().relative_to(cwd.resolve())
        except ValueError:
            pass
    return path.as_posix()


def _ruff_diagnostics(cwd: Path, files: list[str]) -> Counter[Diagnostic]:
    if not files:
        return Counter()
    result = _run(
        [
            sys.executable,
            "-m",
            "ruff",
            "check",
            "--output-format=json",
            *files,
        ],
        cwd=cwd,
        allowed_codes={0, 1},
    )
    payload: list[dict[str, Any]] = json.loads(result.stdout or "[]")
    diagnostics: Counter[Diagnostic] = Counter()
    for item in payload:
        diagnostics[
            (
                _relative_path(item["filename"], cwd),
                str(item.get("code") or "unknown"),
                str(item["message"]),
            )
        ] += 1
    return diagnostics


def _mypy_diagnostics(cwd: Path, files: list[str]) -> Counter[Diagnostic]:
    if not files:
        return Counter()
    result = _run(
        [
            sys.executable,
            "-m",
            "mypy",
            "--follow-imports=silent",
            "--check-untyped-defs",
            "--show-error-codes",
            "--no-pretty",
            "--no-error-summary",
            "--no-incremental",
            *files,
        ],
        cwd=cwd,
        allowed_codes={0, 1},
    )
    diagnostics: Counter[Diagnostic] = Counter()
    for line in (result.stdout + result.stderr).splitlines():
        match = MYPY_ERROR.match(line) or MYPY_FILE_ERROR.match(line)
        if not match:
            continue
        diagnostics[
            (
                _relative_path(match.group("path"), cwd),
                match.group("code") or "unknown",
                match.group("message"),
            )
        ] += 1
    return diagnostics


def _changed_python_files(base: str) -> list[str]:
    result = _run(
        [
            "git",
            "diff",
            "--diff-filter=ACMR",
            "--name-only",
            base,
            "HEAD",
            "--",
            "*.py",
        ],
        cwd=ROOT,
        allowed_codes={0},
    )
    return [line for line in result.stdout.splitlines() if line]


def _base_files(base: str, changed_files: list[str]) -> list[str]:
    files: list[str] = []
    for path in changed_files:
        exists = subprocess.run(
            ["git", "cat-file", "-e", f"{base}:{path}"],
            cwd=ROOT,
            capture_output=True,
            check=False,
        )
        if exists.returncode == 0:
            files.append(path)
    return files


def _print_new(tool: str, diagnostics: Counter[Diagnostic]) -> None:
    for (path, code, message), count in sorted(diagnostics.items()):
        suffix = f" (repeated {count} times)" if count > 1 else ""
        print(f"{tool}: {path}: [{code}] {message}{suffix}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", required=True)
    args = parser.parse_args()

    changed_files = _changed_python_files(args.base)
    if not changed_files:
        print("quality-diff: no changed Python files")
        return 0

    print("quality-diff: checking current Ruff diagnostics", flush=True)
    head_ruff = _ruff_diagnostics(ROOT, changed_files)
    print("quality-diff: checking current Mypy diagnostics", flush=True)
    head_mypy = _mypy_diagnostics(ROOT, changed_files)

    object_type = _run(
        ["git", "cat-file", "-t", args.base],
        cwd=ROOT,
        allowed_codes={0},
    ).stdout.strip()
    base_ruff: Counter[Diagnostic] = Counter()
    base_mypy: Counter[Diagnostic] = Counter()

    if object_type == "commit":
        base_files = _base_files(args.base, changed_files)
        with tempfile.TemporaryDirectory(prefix="backend-ci-base-") as temp_dir:
            base_root = Path(temp_dir) / "worktree"
            _run(
                ["git", "worktree", "add", "--detach", str(base_root), args.base],
                cwd=ROOT,
                allowed_codes={0},
            )
            try:
                print("quality-diff: checking baseline Ruff diagnostics", flush=True)
                base_ruff = _ruff_diagnostics(base_root, base_files)
                print("quality-diff: checking baseline Mypy diagnostics", flush=True)
                base_mypy = _mypy_diagnostics(base_root, base_files)
            finally:
                _run(
                    ["git", "worktree", "remove", "--force", str(base_root)],
                    cwd=ROOT,
                    allowed_codes={0},
                )
    elif object_type != "tree":
        raise RuntimeError(f"Unsupported base object type: {object_type}")

    new_ruff = head_ruff - base_ruff
    new_mypy = head_mypy - base_mypy
    print(
        "quality-diff: "
        f"Ruff current={sum(head_ruff.values())} "
        f"baseline={sum(base_ruff.values())} "
        f"new={sum(new_ruff.values())}; "
        f"Mypy current={sum(head_mypy.values())} "
        f"baseline={sum(base_mypy.values())} "
        f"new={sum(new_mypy.values())}"
    )
    if new_ruff or new_mypy:
        _print_new("Ruff", new_ruff)
        _print_new("Mypy", new_mypy)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
