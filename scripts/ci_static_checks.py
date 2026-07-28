"""Dependency-free Phase 2 lint, type-contract, and import checks."""

from __future__ import annotations

import ast
import importlib
import inspect
import os
import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
EXCLUDED_PARTS = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    "__pycache__",
    "htmlcov",
    "venv",
}
STORE_METHODS = (
    "_search",
    "get_contacted_leads",
    "get_lead",
    "get_all_leads",
    "get_lead_by_id",
    "get_messages_for_lead",
    "add_lead",
    "update_lead_status",
    "update_lead_status_by_id",
    "update_human_takeover_by_id",
    "append_message",
    "update_message_status",
    "update_lead_info",
    "update_lead_score",
)


def python_files() -> list[Path]:
    candidates = [
        *ROOT.joinpath("app").rglob("*.py"),
        *ROOT.joinpath("scripts").rglob("*.py"),
        *ROOT.joinpath("tests", "unit").rglob("*.py"),
        ROOT / "main.py",
        ROOT / "worker.py",
    ]
    return sorted(
        path
        for path in candidates
        if path.is_file() and not EXCLUDED_PARTS.intersection(path.parts)
    )


def check_lint() -> None:
    failures: list[str] = []
    for path in python_files():
        text = path.read_text(encoding="utf-8")
        if re.search(r"^(?:<{7} |={7}$|>{7} )", text, re.MULTILINE):
            failures.append(f"{path.relative_to(ROOT)}: merge-conflict marker")
        try:
            ast.parse(text, filename=str(path))
        except SyntaxError as exc:
            failures.append(
                f"{path.relative_to(ROOT)}:{exc.lineno}: {exc.msg}"
            )
    if failures:
        raise SystemExit("Lint check failed:\n" + "\n".join(failures))
    print(f"lint: parsed {len(python_files())} Python files")


def check_imports_and_store_types() -> None:
    os.environ.update(
        {
            "APP_ENV": "ci",
            "AIRTABLE_API_KEY": "",
            "AIRTABLE_BASE_ID": "",
            "DATABASE_URL": "",
            "GEMINI_API_KEY": "",
            "NINEROUTER_API_KEY": "",
            "REDIS_URL": "",
            "SENTRY_DSN": "",
            "WHATSAPP_ACCESS_TOKEN": "",
            "WHATSAPP_PHONE_NUMBER_ID": "",
            "WHATSAPP_APP_SECRET": "offline-ci-secret",
            "WHATSAPP_VERIFY_TOKEN": "offline-ci-token",
            "MIGRATION_MODE": "airtable",
        }
    )
    for module_name in (
        "app.api.routers.leads",
        "app.api.routers.whatsapp",
        "app.services.jobs",
        "app.store.store",
        "main",
        "worker",
    ):
        importlib.import_module(module_name)
    print("imports: critical backend modules imported with offline CI settings")

    from app.clients.airtable_client import AirtableClient
    from app.store.db_client import DatabaseClient
    from app.store.store import DualWriteStore

    failures: list[str] = []
    for implementation in (AirtableClient, DatabaseClient, DualWriteStore):
        for method_name in STORE_METHODS:
            method = getattr(implementation, method_name, None)
            if method is None:
                failures.append(f"{implementation.__name__}.{method_name}: missing")
                continue
            parameter = inspect.signature(method).parameters.get("client_id")
            if parameter is None or parameter.default is not inspect.Parameter.empty:
                failures.append(
                    f"{implementation.__name__}.{method_name}: client_id must be required"
                )
            elif parameter.annotation is inspect.Parameter.empty:
                failures.append(
                    f"{implementation.__name__}.{method_name}: client_id must be typed"
                )
    if failures:
        raise SystemExit("Store type-contract check failed:\n" + "\n".join(failures))
    print("types: concrete lead-store client_id contract is required and annotated")


if __name__ == "__main__":
    check_lint()
    check_imports_and_store_types()
