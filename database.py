# Backward-compat shim — remove after Phase 8 (main.py imports updated).
from app.core.database import *  # noqa: F401, F403
from app.core.database import Base, engine, SessionLocal, init_engine, is_configured, get_db  # noqa: F401
