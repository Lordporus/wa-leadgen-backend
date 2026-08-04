"""Durable, versioned Phase 12A WhatsApp operational controls."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.exc import IntegrityError

from app.core import config, database
from app.core.models import (
    Client,
    WhatsAppOperationalControl,
    WhatsAppOperationalControlAudit,
    WhatsAppSequence,
    WhatsAppTemplate,
)

GLOBAL_OUTBOUND = "global_outbound"
TENANT_OUTBOUND = "tenant_outbound"
AI_AUTO_REPLY = "ai_auto_reply"
SEQUENCE = "sequence"
TEMPLATE = "template"
WORKER_CONSUMPTION = "worker_consumption"

GLOBAL_CONTROLS = frozenset({GLOBAL_OUTBOUND, WORKER_CONSUMPTION})
TENANT_CONTROLS = frozenset(
    {TENANT_OUTBOUND, AI_AUTO_REPLY, SEQUENCE, TEMPLATE}
)


class OperationalControlError(RuntimeError):
    """A fail-closed operational-control validation or persistence error."""


class OperationalControlConflict(OperationalControlError):
    """The requested transition is stale or reuses another correlation ID."""


@dataclass(frozen=True)
class ControlState:
    control: str
    enabled: bool
    effective_enabled: bool
    version: int
    scope: str
    client_id: int | None
    resource_id: int | None
    updated_by: str | None
    reason: str | None
    correlation_id: str | None
    updated_at: datetime | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "control": self.control,
            "enabled": self.enabled,
            "effective_enabled": self.effective_enabled,
            "version": self.version,
            "scope": self.scope,
            "client_id": self.client_id,
            "resource_id": self.resource_id,
            "updated_by": self.updated_by,
            "reason": self.reason,
            "correlation_id": self.correlation_id,
            "updated_at": self.updated_at,
        }


def _factory():
    if database.SessionLocal is None:
        raise OperationalControlError(
            "WhatsApp operational controls require the durable database"
        )
    return database.SessionLocal


def _key(
    control: str, *, client_id: int | None, resource_id: int | None
) -> tuple[str, str]:
    if control in GLOBAL_CONTROLS:
        if client_id is not None or resource_id is not None:
            raise OperationalControlError(
                "Global controls cannot include tenant or resource IDs"
            )
        return "global", f"global:{control}"
    if control not in TENANT_CONTROLS or client_id is None:
        raise OperationalControlError("Unknown or incomplete tenant control")
    if control in {SEQUENCE, TEMPLATE}:
        if resource_id is None or resource_id <= 0:
            raise OperationalControlError(
                f"{control} control requires a positive resource_id"
            )
    elif resource_id is not None:
        raise OperationalControlError(
            f"{control} control does not accept resource_id"
        )
    suffix = f":{resource_id}" if resource_id is not None else ""
    return "tenant", f"tenant:{client_id}:{control}{suffix}"


def _static_ceiling(control: str) -> bool:
    if control == GLOBAL_OUTBOUND:
        return config.WHATSAPP_OUTBOUND_ENABLED
    if control == WORKER_CONSUMPTION:
        return config.WHATSAPP_RQ_CONSUMER_ENABLED
    return True


def _state(
    row: WhatsAppOperationalControl | None,
    *,
    control: str,
    scope: str,
    client_id: int | None,
    resource_id: int | None,
) -> ControlState:
    configured = True if row is None else bool(row.enabled)
    return ControlState(
        control=control,
        enabled=configured,
        effective_enabled=configured and _static_ceiling(control),
        version=0 if row is None else row.version,
        scope=scope,
        client_id=client_id,
        resource_id=resource_id,
        updated_by=None if row is None else row.updated_by,
        reason=None if row is None else row.reason,
        correlation_id=None if row is None else row.correlation_id,
        updated_at=None if row is None else row.updated_at,
    )


def enabled_locked(
    session,
    control: str,
    *,
    client_id: int | None = None,
    resource_id: int | None = None,
    lock: bool = False,
) -> bool:
    """Return effective state; database failures propagate and therefore block."""
    scope, control_key = _key(
        control, client_id=client_id, resource_id=resource_id
    )
    query = session.query(WhatsAppOperationalControl).filter_by(
        control_key=control_key
    )
    if lock:
        query = query.with_for_update()
    row = query.one_or_none()
    return _state(
        row,
        control=control,
        scope=scope,
        client_id=client_id,
        resource_id=resource_id,
    ).effective_enabled


def enabled(
    control: str,
    *,
    client_id: int | None = None,
    resource_id: int | None = None,
) -> bool:
    with _factory()() as session:
        return enabled_locked(
            session,
            control,
            client_id=client_id,
            resource_id=resource_id,
        )


def _validate_target_locked(
    session,
    *,
    control: str,
    client_id: int | None,
    resource_id: int | None,
) -> None:
    if client_id is None:
        return
    client = (
        session.query(Client)
        .filter_by(id=client_id)
        .with_for_update()
        .one_or_none()
    )
    if client is None:
        raise OperationalControlError("Tenant does not exist")
    if control == SEQUENCE:
        target = session.query(WhatsAppSequence).filter_by(
            id=resource_id, client_id=client_id
        ).one_or_none()
        if target is None:
            raise OperationalControlError(
                "Sequence does not belong to this tenant"
            )
    elif control == TEMPLATE:
        target = session.query(WhatsAppTemplate).filter_by(
            id=resource_id, client_id=client_id
        ).one_or_none()
        if target is None:
            raise OperationalControlError(
                "Template does not belong to this tenant"
            )


def mutate(
    *,
    control: str,
    enabled_value: bool,
    expected_version: int,
    operator_id: str,
    reason: str,
    correlation_id: str,
    client_id: int | None = None,
    resource_id: int | None = None,
) -> ControlState:
    """Atomically transition current state and append exactly one audit row."""
    if expected_version < 0:
        raise OperationalControlError("expected_version cannot be negative")
    if not operator_id.strip() or not reason.strip() or not correlation_id.strip():
        raise OperationalControlError(
            "operator, reason, and correlation_id are required"
        )
    scope, control_key = _key(
        control, client_id=client_id, resource_id=resource_id
    )
    try:
        with _factory()() as session:
            prior_audit = (
                session.query(WhatsAppOperationalControlAudit)
                .filter_by(correlation_id=correlation_id)
                .one_or_none()
            )
            if prior_audit is not None:
                if (
                    prior_audit.control_key != control_key
                    or prior_audit.to_enabled != enabled_value
                ):
                    raise OperationalControlConflict(
                        "correlation_id was already used for another transition"
                    )
                row = session.query(WhatsAppOperationalControl).filter_by(
                    id=prior_audit.control_id
                ).one()
                return _state(
                    row,
                    control=control,
                    scope=scope,
                    client_id=client_id,
                    resource_id=resource_id,
                )

            _validate_target_locked(
                session,
                control=control,
                client_id=client_id,
                resource_id=resource_id,
            )
            row = (
                session.query(WhatsAppOperationalControl)
                .filter_by(control_key=control_key)
                .with_for_update()
                .one_or_none()
            )
            current_version = 0 if row is None else row.version
            if current_version != expected_version:
                raise OperationalControlConflict(
                    f"stale control version: expected {expected_version}, "
                    f"current {current_version}"
                )
            previous = None if row is None else bool(row.enabled)
            next_version = current_version + 1
            now = datetime.now(timezone.utc)
            if row is None:
                row = WhatsAppOperationalControl(
                    control_key=control_key,
                    scope=scope,
                    client_id=client_id,
                    control_type=control,
                    resource_id=resource_id,
                    enabled=enabled_value,
                    version=next_version,
                    updated_by=operator_id.strip(),
                    reason=reason.strip(),
                    correlation_id=correlation_id,
                    created_at=now,
                    updated_at=now,
                )
                session.add(row)
                session.flush()
            else:
                row.enabled = enabled_value
                row.version = next_version
                row.updated_by = operator_id.strip()
                row.reason = reason.strip()
                row.correlation_id = correlation_id
                row.updated_at = now
            session.add(
                WhatsAppOperationalControlAudit(
                    control_id=row.id,
                    control_key=control_key,
                    scope=scope,
                    client_id=client_id,
                    control_type=control,
                    resource_id=resource_id,
                    from_enabled=previous,
                    to_enabled=enabled_value,
                    from_version=current_version,
                    to_version=next_version,
                    operator_id=operator_id.strip(),
                    reason=reason.strip(),
                    correlation_id=correlation_id,
                    created_at=now,
                )
            )
            session.commit()
            session.refresh(row)
            return _state(
                row,
                control=control,
                scope=scope,
                client_id=client_id,
                resource_id=resource_id,
            )
    except IntegrityError as exc:
        raise OperationalControlConflict(
            "Concurrent operational-control transition"
        ) from exc


def list_states(*, client_id: int | None) -> list[dict[str, Any]]:
    controls = (
        sorted(GLOBAL_CONTROLS)
        if client_id is None
        else sorted({TENANT_OUTBOUND, AI_AUTO_REPLY})
    )
    with _factory()() as session:
        rows = session.query(WhatsAppOperationalControl).filter_by(
            client_id=client_id
        ).order_by(
            WhatsAppOperationalControl.control_type,
            WhatsAppOperationalControl.resource_id,
        ).all()
        by_type = {
            (row.control_type, row.resource_id): row for row in rows
        }
        result = []
        scope = "global" if client_id is None else "tenant"
        for control in controls:
            result.append(
                _state(
                    by_type.get((control, None)),
                    control=control,
                    scope=scope,
                    client_id=client_id,
                    resource_id=None,
                ).as_dict()
            )
        for row in rows:
            if row.resource_id is not None:
                result.append(
                    _state(
                        row,
                        control=row.control_type,
                        scope=row.scope,
                        client_id=client_id,
                        resource_id=row.resource_id,
                    ).as_dict()
                )
        return result


def sync_worker_suspension(*, enabled_value: bool) -> bool:
    """Best-effort Redis suspension; the database gate remains authoritative."""
    from rq.suspension import resume, suspend

    from app.api.runtime import redis_conn

    if redis_conn is None:
        return False
    if enabled_value:
        resume(redis_conn)
    else:
        suspend(redis_conn)
    return True
