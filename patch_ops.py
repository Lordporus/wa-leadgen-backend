import re

with open("app/services/whatsapp_operations.py", "r", encoding="utf-8") as f:
    content = f.read()

mutation_req = """
@dataclass(frozen=True)
class MutationRequest:
    control: str
    enabled_value: bool
    expected_version: int
"""
content = re.sub(r"(class ControlState:[\s\S]*?    def as_dict.*?        return {[\s\S]*?        })", r"\1\n" + mutation_req, content)

mutate_func = """

def mutate_multiple(
    *,
    requests: list[MutationRequest],
    operator_id: str,
    reason: str,
    correlation_id: str,
    client_id: int | None = None,
    resource_id: int | None = None,
) -> dict[str, ControlState]:
    \"\"\"Atomically transition multiple operational controls and append audit rows.\"\"\"
    if not operator_id.strip() or not reason.strip() or not correlation_id.strip():
        raise OperationalControlError(
            "operator, reason, and correlation_id are required"
        )
    for req in requests:
        if req.expected_version < 0:
            raise OperationalControlError("expected_version cannot be negative")

    sorted_reqs = sorted(requests, key=lambda r: _key(r.control, client_id=client_id, resource_id=resource_id)[1])
    
    try:
        with _factory()() as session:
            first_corr = f"{correlation_id}:{sorted_reqs[0].control}"
            prior_audit = (
                session.query(WhatsAppOperationalControlAudit)
                .filter_by(correlation_id=first_corr)
                .one_or_none()
            )
            if prior_audit is not None:
                results = {}
                for req in requests:
                    scope, control_key = _key(req.control, client_id=client_id, resource_id=resource_id)
                    row = session.query(WhatsAppOperationalControl).filter_by(control_key=control_key).one()
                    results[req.control] = _state(row, control=req.control, scope=scope, client_id=client_id, resource_id=resource_id)
                return results

            rows_by_req = {}
            for req in sorted_reqs:
                scope, control_key = _key(req.control, client_id=client_id, resource_id=resource_id)
                _validate_target_locked(session, control=req.control, client_id=client_id, resource_id=resource_id)
                row = (
                    session.query(WhatsAppOperationalControl)
                    .filter_by(control_key=control_key)
                    .with_for_update()
                    .one_or_none()
                )
                current_version = 0 if row is None else row.version
                if current_version != req.expected_version:
                    raise OperationalControlConflict(
                        f"stale control version for {req.control}: expected {req.expected_version}, "
                        f"current {current_version}"
                    )
                rows_by_req[req] = (row, scope, control_key, current_version)

            for req, (row, scope, control_key, current_version) in rows_by_req.items():
                previous = None if row is None else bool(row.enabled)
                next_version = current_version + 1
                now = datetime.now(timezone.utc)
                if row is None:
                    row = WhatsAppOperationalControl(
                        control_key=control_key,
                        scope=scope,
                        client_id=client_id,
                        control_type=req.control,
                        resource_id=resource_id,
                        enabled=req.enabled_value,
                        version=next_version,
                        updated_by=operator_id.strip(),
                        reason=reason.strip(),
                        correlation_id=f"{correlation_id}:{req.control}",
                        created_at=now,
                        updated_at=now,
                    )
                    session.add(row)
                    session.flush()
                else:
                    row.enabled = req.enabled_value
                    row.version = next_version
                    row.updated_by = operator_id.strip()
                    row.reason = reason.strip()
                    row.correlation_id = f"{correlation_id}:{req.control}"
                    row.updated_at = now
                session.add(
                    WhatsAppOperationalControlAudit(
                        control_id=row.id,
                        control_key=control_key,
                        scope=scope,
                        client_id=client_id,
                        control_type=req.control,
                        resource_id=resource_id,
                        from_enabled=previous,
                        to_enabled=req.enabled_value,
                        from_version=current_version,
                        to_version=next_version,
                        operator_id=operator_id.strip(),
                        reason=reason.strip(),
                        correlation_id=f"{correlation_id}:{req.control}",
                        created_at=now,
                    )
                )
                rows_by_req[req] = row

            session.commit()
            
            final_results = {}
            for req, row in rows_by_req.items():
                session.refresh(row)
                scope, control_key = _key(req.control, client_id=client_id, resource_id=resource_id)
                final_results[req.control] = _state(
                    row,
                    control=req.control,
                    scope=scope,
                    client_id=client_id,
                    resource_id=resource_id,
                )
            return final_results
    except IntegrityError as exc:
        raise OperationalControlConflict(
            "Concurrent operational-control transition"
        ) from exc
"""
content = content + "\n" + mutate_func

with open("app/services/whatsapp_operations.py", "w", encoding="utf-8") as f:
    f.write(content)
