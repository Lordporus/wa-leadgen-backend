"""Run one approved Alembic release while holding an advisory lock."""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, text


# Stable, application-specific signed bigint used only to serialize migrations.
MIGRATION_LOCK_ID = 627815706193221774


def _configured_head(alembic_config: Config) -> str:
    """Return the sole repository head, rejecting an ambiguous graph."""
    heads = ScriptDirectory.from_config(alembic_config).get_heads()
    if len(heads) != 1:
        raise RuntimeError("Alembic must have exactly one configured head")
    return heads[0]


def run_migrations(
    database_url: str | None = None,
    *,
    expected_current_revision: str | None = None,
    expected_target_revision: str | None = None,
    backup_verified: bool = False,
    approval_id: str | None = None,
) -> None:
    """Apply the repository head only after explicit release preflight checks."""
    url = database_url or os.getenv("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL is required before running migrations")
    if not expected_current_revision:
        raise RuntimeError("expected current Alembic revision is required")
    if not expected_target_revision:
        raise RuntimeError("expected target Alembic revision is required")
    if not backup_verified:
        raise RuntimeError("verified backup/recovery evidence is required")
    if not approval_id:
        raise RuntimeError("release approval identifier is required")

    engine = create_engine(url, pool_pre_ping=True, future=True)
    try:
        with engine.begin() as connection:
            connection.execute(
                text("SELECT pg_advisory_xact_lock(:lock_id)"),
                {"lock_id": MIGRATION_LOCK_ID},
            )
            print("Acquired PostgreSQL migration advisory lock.", flush=True)
            alembic_config = Config(
                str(Path(__file__).resolve().parents[1] / "alembic.ini")
            )
            head = _configured_head(alembic_config)
            if head != expected_target_revision:
                raise RuntimeError(
                    "refusing migration: expected target revision "
                    f"{expected_target_revision!r}, found {head!r}"
                )
            current_revision = connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one_or_none()
            if current_revision != expected_current_revision:
                raise RuntimeError(
                    "refusing migration: expected current revision "
                    f"{expected_current_revision!r}, found {current_revision!r}"
                )
            print(
                f"Migration preflight passed: current={current_revision}, target={head}, "
                f"approval={approval_id}",
                flush=True,
            )
            alembic_config.attributes["connection"] = connection
            command.upgrade(alembic_config, head)
            print("Alembic migration gate completed.", flush=True)
    finally:
        engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run one approved Alembic release")
    parser.add_argument("--expected-current-revision", required=True)
    parser.add_argument("--expected-target-revision", required=True)
    parser.add_argument("--backup-verified", action="store_true")
    parser.add_argument("--approval-id", required=True)
    args = parser.parse_args()
    run_migrations(
        expected_current_revision=args.expected_current_revision,
        expected_target_revision=args.expected_target_revision,
        backup_verified=args.backup_verified,
        approval_id=args.approval_id,
    )
