import sys
from logging.config import fileConfig
from pathlib import Path

from sqlalchemy import engine_from_config, pool
from alembic import context

# ---------------------------------------------------------------------------
# Make sure the backend package root is on sys.path so we can import
# config, database, and models directly (same as running main.py from
# the backend/ directory).
# ---------------------------------------------------------------------------
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import DATABASE_URL          # noqa: E402
from app.core.database import Base                # noqa: E402
import app.core.models                            # noqa: E402, F401  — registers all tables on Base.metadata

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode — emits SQL to stdout."""
    url = DATABASE_URL
    if not url:
        raise RuntimeError(
            "DATABASE_URL is not set. Cannot generate offline migrations."
        )
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode — connects to the database."""
    url = DATABASE_URL
    if not url:
        raise RuntimeError(
            "DATABASE_URL is not set. Cannot run online migrations."
        )

    cfg = config.get_section(config.config_ini_section, {})
    cfg["sqlalchemy.url"] = url

    connectable = engine_from_config(
        cfg,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
