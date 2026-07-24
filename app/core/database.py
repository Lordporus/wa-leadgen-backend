"""
Phase 7 — Postgres (Supabase) database engine & session factory.

SQLAlchemy 2.0 engine tuned for Render's free tier:
  - small pool (free tier caps connections)
  - pool_pre_ping so dropped/stale connections are recycled transparently
  - connection URL comes from DATABASE_URL (Supabase connection pooler URI)
"""

import logging
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base

logger = logging.getLogger(__name__)

Base = declarative_base()

# Module-level engine. Created lazily / only meaningful when DATABASE_URL is set.
engine = None
SessionLocal = None


def init_engine(database_url: str | None):
    """Initialise the engine + session factory. Safe to call once at startup.

    Renders the module-level `engine` / `SessionLocal`. Subsequent DB clients
    should call :func:`is_configured` before touching the engine.
    """
    global engine, SessionLocal
    if not database_url:
        logger.warning("DATABASE_URL not set — Postgres layer disabled.")
        return
    engine = create_engine(
        database_url,
        pool_size=3,
        max_overflow=2,
        pool_timeout=30,
        pool_recycle=300,
        pool_pre_ping=True,
        future=True,
    )
    SessionLocal = sessionmaker(bind=engine, autoflush=False, future=True)
    logger.info("Postgres engine initialised.")


def is_configured() -> bool:
    """True if init_engine() has successfully prepared a usable engine."""
    return engine is not None and SessionLocal is not None


def get_db():
    """FastAPI dependency yielding a session. Always closes on exit."""
    if not is_configured():
        # No engine configured → yield None so callers can no-op gracefully.
        # (DB-backed endpoints should guard on is_configured() themselves.)
        yield None
        return
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
