import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.models import Client


def test_only_one_active_tenant_can_own_a_whatsapp_phone_id():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Client.__table__.create(engine)
    try:
        with Session(engine) as session:
            session.add(Client(name="Tenant A", wa_phone_number_id="phone-id", is_active=True))
            session.commit()
            session.add(Client(name="Tenant B", wa_phone_number_id="phone-id", is_active=True))
            with pytest.raises(IntegrityError):
                session.commit()
            session.rollback()

            session.add(Client(name="Inactive tenant", wa_phone_number_id="phone-id", is_active=False))
            session.commit()
    finally:
        Client.__table__.drop(engine)
        engine.dispose()


def test_whatsapp_phone_id_must_be_trimmed():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Client.__table__.create(engine)
    try:
        with Session(engine) as session:
            session.add(Client(name="Tenant", wa_phone_number_id=" phone-id ", is_active=True))
            with pytest.raises(IntegrityError):
                session.commit()
    finally:
        Client.__table__.drop(engine)
        engine.dispose()
