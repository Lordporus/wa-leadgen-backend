"""Run Alembic while holding a PostgreSQL session advisory lock."""

from __future__ import annotations

import os
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text


# Stable, application-specific signed bigint used only to serialize migrations.
MIGRATION_LOCK_ID = 627815706193221774


def run_migrations(database_url: str | None = None) -> None:
    """Serialize Alembic across concurrently starting service instances."""
    url = database_url or os.getenv("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL is required before running migrations")

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
            alembic_config.attributes["connection"] = connection
            command.upgrade(alembic_config, "head")
            print("Alembic migration gate completed.", flush=True)
    finally:
        engine.dispose()


if __name__ == "__main__":
    run_migrations()
