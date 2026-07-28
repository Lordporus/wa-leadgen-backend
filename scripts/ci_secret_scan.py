"""Small dependency-free secret scanner for CI and offline developer use."""

from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCAN_ROOTS = (
    ROOT / ".github",
    ROOT / "alembic",
    ROOT / "app",
    ROOT / "docs",
    ROOT / "scripts",
    ROOT / "tests",
)
ROOT_FILES = (
    ROOT / ".env.example",
    ROOT / ".env.staging.example",
    ROOT / "main.py",
    ROOT / "render.yaml",
    ROOT / "worker.py",
)
EXCLUDED_PARTS = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    "__pycache__",
    "htmlcov",
    "node_modules",
    "venv",
}
TEXT_SUFFIXES = {
    ".ini",
    ".json",
    ".md",
    ".py",
    ".toml",
    ".txt",
    ".yaml",
    ".yml",
}
PATTERNS = (
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(r"\b(?:sk|rk)-[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\b(?:ghp_|github_pat_)[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{20,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(
        r"""(?ix)
        \b(?:api[_-]?key|access[_-]?token|client[_-]?secret|password|secret)
        \s*[:=]\s*["']?
        ([A-Za-z0-9_./+=-]{20,})
        """
    ),
)
SAFE_MARKERS = (
    "${{",
    "change-me",
    "changeme",
    "dummy",
    "example",
    "fake",
    "offline",
    "placeholder",
    "process.env",
    "test-",
    "your_",
)


def iter_files():
    for scan_root in SCAN_ROOTS:
        if scan_root.exists():
            for path in scan_root.rglob("*"):
                if (
                    path.is_file()
                    and path.suffix.lower() in TEXT_SUFFIXES
                    and not EXCLUDED_PARTS.intersection(path.parts)
                ):
                    yield path
    for path in ROOT_FILES:
        if path.is_file():
            yield path


def main() -> int:
    findings: list[str] = []
    for path in sorted(set(iter_files())):
        for line_number, line in enumerate(
            path.read_text(encoding="utf-8", errors="replace").splitlines(),
            start=1,
        ):
            matches = [pattern.search(line) for pattern in PATTERNS]
            matches = [match for match in matches if match is not None]
            if matches:
                unsafe = False
                for match in matches:
                    captured = (
                        match.group(1)
                        if match.lastindex and match.group(1)
                        else ""
                    )
                    if not captured:
                        unsafe = True
                        break
                    lowered = captured.lower()
                    if re.fullmatch(r"[A-Z][A-Z0-9_]+", captured):
                        continue
                    if any(marker in lowered for marker in SAFE_MARKERS):
                        continue
                    unsafe = True
                    break
                if unsafe:
                    findings.append(f"{path.relative_to(ROOT)}:{line_number}")
    if findings:
        print("Potential secrets found (values suppressed):")
        print("\n".join(findings))
        return 1
    print("secret-scan: no credential-shaped values found")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
