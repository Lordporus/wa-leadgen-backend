import re

with open("app/services/whatsapp_pilot.py", "r", encoding="utf-8") as f:
    content = f.read()

# Replace final_send_gate_locked
gate_old = """    # Fast-pass 3: gate only applies to the approved pilot tenant.
    try:
        pilot_tenant_id = int(config.WHATSAPP_PILOT_TENANT_ID or "")
    except (TypeError, ValueError):
        return None"""
gate_new = """    # Fast-pass 3: gate only applies to the approved pilot tenant.
    try:
        pilot_tenant_id = int(config.WHATSAPP_PILOT_TENANT_ID or "")
    except (TypeError, ValueError):
        return "pilot_prerequisite_missing_or_stale\""""

content = content.replace(gate_old, gate_new)

# Replace transition_stage
ts_pattern = re.compile(r"def transition_stage\([\s\S]*?\)\s*->\s*dict\[str,\s*Any\]:[\s\S]*?return \{.*?\}", re.MULTILINE)
ts_new = """def transition_stage(
    *,
    client_id: int,
    expected_stage: int,
    target_stage: int,
    expected_version_stage_2: int,
    expected_version_stage_3: int,
    operator_id: str,
    reason: str,
    correlation_id: str,
) -> dict[str, Any]:
    if database.SessionLocal is None:
        raise PilotError("pilot controls require the durable database")
    if target_stage not in {1, 2, 3} or abs(target_stage - expected_stage) != 1:
        raise PilotConflict("pilot stages must move exactly one stage at a time")
    with database.SessionLocal() as session:
        current = _current_stage_locked(session, client_id)
        enabled = whatsapp_operations.state_locked(
            session,
            whatsapp_operations.PILOT_ENABLED,
            client_id=client_id,
            lock=False,
        ).enabled
        readiness = readiness_locked(session, client_id=client_id)
    if current != expected_stage:
        raise PilotConflict(
            f"stale pilot stage: expected {expected_stage}, current {current}"
        )
    if target_stage > current and (not enabled or not readiness.ready):
        raise PilotConflict("pilot must be enabled and ready before stage expansion")

    requests = [
        whatsapp_operations.MutationRequest(
            control=whatsapp_operations.PILOT_STAGE_2,
            enabled_value=target_stage >= 2,
            expected_version=expected_version_stage_2,
        ),
        whatsapp_operations.MutationRequest(
            control=whatsapp_operations.PILOT_STAGE_3,
            enabled_value=target_stage >= 3,
            expected_version=expected_version_stage_3,
        )
    ]
    try:
        states = whatsapp_operations.mutate_multiple(
            requests=requests,
            operator_id=operator_id,
            reason=reason,
            correlation_id=correlation_id,
            client_id=client_id,
        )
    except whatsapp_operations.OperationalControlConflict as e:
        raise PilotConflict(str(e)) from e

    s2 = states[whatsapp_operations.PILOT_STAGE_2].enabled
    s3 = states[whatsapp_operations.PILOT_STAGE_3].enabled
    if s3 and not s2:
        raise PilotError("pilot stage corruption: stage 3 enabled without stage 2")
    
    return {
        "stage": target_stage,
        "controls": {
            k: v.as_dict() for k, v in states.items()
        }
    }"""

content = ts_pattern.sub(ts_new, content)

with open("app/services/whatsapp_pilot.py", "w", encoding="utf-8") as f:
    f.write(content)
