from contextlib import nullcontext
from unittest.mock import MagicMock

from scripts import run_migrations


def test_migrations_run_while_postgres_advisory_lock_is_held(monkeypatch):
    events = []
    connection = MagicMock()
    connection.execute.side_effect = lambda statement, params: events.append(
        (str(statement), params)
    )
    engine = MagicMock()
    engine.begin.return_value = nullcontext(connection)

    monkeypatch.setattr(run_migrations, "create_engine", lambda *args, **kwargs: engine)
    upgrade = MagicMock(
        side_effect=lambda config, revision: events.append(
            ("alembic", config.attributes["connection"], revision)
        )
    )
    monkeypatch.setattr(
        run_migrations.command,
        "upgrade",
        upgrade,
    )

    run_migrations.run_migrations("postgresql://offline/never-connected")

    assert "pg_advisory_xact_lock" in events[0][0]
    assert events[1][0] == "alembic"
    assert events[1][1] is connection
    assert events[1][2] == "head"
    engine.dispose.assert_called_once_with()


def test_migration_failure_exits_transaction_and_disposes_engine(monkeypatch):
    events = []
    connection = MagicMock()
    connection.execute.side_effect = lambda statement, params: events.append(
        str(statement)
    )
    engine = MagicMock()
    engine.begin.return_value = nullcontext(connection)

    monkeypatch.setattr(run_migrations, "create_engine", lambda *args, **kwargs: engine)

    def fail(*args, **kwargs):
        raise RuntimeError("offline migration failure")

    monkeypatch.setattr(run_migrations.command, "upgrade", fail)

    try:
        run_migrations.run_migrations("postgresql://offline/never-connected")
    except RuntimeError:
        pass

    assert "pg_advisory_xact_lock" in events[0]
    engine.dispose.assert_called_once_with()
