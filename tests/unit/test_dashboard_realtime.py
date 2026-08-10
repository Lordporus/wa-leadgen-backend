import asyncio

from fastapi.routing import APIRoute
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.api.dependencies import verify_jwt
from app.api.routers.realtime import _encode_event, router
from app.core.database import Base
from app.core.models import Client, Lead
from app.services import dashboard_events


def test_dashboard_event_publishes_only_after_commit(monkeypatch):
    published = []
    monkeypatch.setattr(dashboard_events, "_publish_committed_event", published.append)
    session = Session()

    queued = dashboard_events.queue_dashboard_event(
        session, "lead_updated", client_id=7, lead_id=42
    )
    assert published == []

    session.commit()
    assert published == [queued]


def test_dashboard_event_is_discarded_after_rollback(monkeypatch):
    published = []
    monkeypatch.setattr(dashboard_events, "_publish_committed_event", published.append)
    session = Session()
    session.begin()
    dashboard_events.queue_dashboard_event(
        session, "lead_created", client_id=7, lead_id=42
    )

    session.rollback()
    session.commit()
    assert published == []


def test_direct_lead_create_and_update_are_captured_after_commit(monkeypatch):
    published = []
    monkeypatch.setattr(dashboard_events, "_publish_committed_event", published.append)
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine, tables=[Client.__table__, Lead.__table__])
    factory = sessionmaker(bind=engine)
    with factory() as session:
        session.add(Client(id=7, name="Tenant 7"))
        session.commit()
        lead = Lead(
            client_id=7,
            name="Safe test lead",
            phone="15550000007",
            status="New Lead",
        )
        session.add(lead)
        session.commit()
        lead.name = "Updated safe test lead"
        session.commit()

    assert [event.event_type for event in published] == [
        "lead_created",
        "lead_updated",
    ]


def test_public_sse_payload_is_minimal_and_has_no_tenant_or_pii():
    event = dashboard_events.new_dashboard_event(
        "lead_stage_changed",
        client_id=7,
        lead_id=42,
        previous_stage="New Lead",
        new_stage="Booked",
    )

    encoded = _encode_event(event)
    assert "event: lead_stage_changed" in encoded
    assert '"lead_id":42' in encoded
    assert '"previous_stage":"New Lead"' in encoded
    assert '"new_stage":"Booked"' in encoded
    assert "client_id" not in encoded
    assert "phone" not in encoded
    assert "email" not in encoded


def test_local_bus_delivers_only_to_matching_tenant():
    async def scenario():
        bus = dashboard_events._LocalTenantBus()
        tenant_one = bus.subscribe(1)
        tenant_two = bus.subscribe(2)
        event = dashboard_events.new_dashboard_event(
            "lead_updated", client_id=1, lead_id=42
        )
        try:
            bus.publish(event)
            await asyncio.sleep(0)
            assert tenant_one[1].get_nowait() == event
            assert tenant_two[1].empty()
        finally:
            bus.unsubscribe(1, tenant_one)
            bus.unsubscribe(2, tenant_two)

    asyncio.run(scenario())


def test_sse_route_uses_verified_jwt_dependency():
    route = next(
        route
        for route in router.routes
        if isinstance(route, APIRoute) and route.path == "/api/dashboard/events"
    )
    assert any(
        dependency.call is verify_jwt for dependency in route.dependant.dependencies
    )
