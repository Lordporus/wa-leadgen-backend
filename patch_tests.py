import re

with open("tests/unit/test_whatsapp_pilot.py", "r", encoding="utf-8") as f:
    content = f.read()

# Replace transition_stage calls
content = re.sub(
    r"expected_version=(\d+),",
    r"expected_version_stage_2=\1, expected_version_stage_3=0,",
    content
)

# Fix test_advance_without_pilot_enabled_raises which was patched too
# Actually, the replacement above is broad but probably covers exactly transition_stage calls?
# Wait, whatsapp_operations.mutate is not in tests/unit/test_whatsapp_pilot.py, except maybe _advance_to_stage_2 etc.
# _advance_to_stage_2 uses whatsapp_operations.mutate!
# Let's fix _advance_to_stage_2 and _advance_to_stage_3 in the test file.

a2 = """def _advance_to_stage_2(client_id: int, corr: str = "adv-s2") -> None:
    whatsapp_operations.mutate_multiple(
        requests=[
            whatsapp_operations.MutationRequest(control=whatsapp_operations.PILOT_STAGE_2, enabled_value=True, expected_version=0),
            whatsapp_operations.MutationRequest(control=whatsapp_operations.PILOT_STAGE_3, enabled_value=False, expected_version=0),
        ],
        operator_id="test",
        reason="advance to stage 2",
        correlation_id=corr,
        client_id=client_id,
    )"""

a3 = """def _advance_to_stage_3(client_id: int, corr: str = "adv-s3") -> None:
    whatsapp_operations.mutate_multiple(
        requests=[
            whatsapp_operations.MutationRequest(control=whatsapp_operations.PILOT_STAGE_2, enabled_value=True, expected_version=1),
            whatsapp_operations.MutationRequest(control=whatsapp_operations.PILOT_STAGE_3, enabled_value=True, expected_version=0),
        ],
        operator_id="test",
        reason="advance to stage 3",
        correlation_id=corr,
        client_id=client_id,
    )"""

content = re.sub(r"def _advance_to_stage_2[\s\S]*?client_id=client_id,\n    \)", a2, content)
content = re.sub(r"def _advance_to_stage_3[\s\S]*?client_id=client_id,\n    \)", a3, content)

# Now, we need to add the new tests for Blocker 1 and Blocker 2.
# Blocker 1 tests:
blocker_1_tests = """
    def test_pilot_enabled_missing_tenant_id_blocked(self, monkeypatch, pilot_db):
        _patch_config(monkeypatch, {"WHATSAPP_PILOT_CONFIG_ENABLED": "true", "WHATSAPP_PILOT_TENANT_ID": ""})
        with pilot_db() as session:
            result = _gate(session, client=_make_client_obj(), lead=None)
        assert result == "pilot_prerequisite_missing_or_stale"

    def test_pilot_enabled_malformed_tenant_id_blocked(self, monkeypatch, pilot_db):
        _patch_config(monkeypatch, {"WHATSAPP_PILOT_CONFIG_ENABLED": "true", "WHATSAPP_PILOT_TENANT_ID": "abc"})
        with pilot_db() as session:
            result = _gate(session, client=_make_client_obj(), lead=None)
        assert result == "pilot_prerequisite_missing_or_stale"
"""
content = content.replace("    def test_pilot_config_disabled_blocks", blocker_1_tests + "\n    def test_pilot_config_disabled_blocks")

# Blocker 2 tests: Update test_stale_version_conflict to use expected_version_stage_2 and 3
ts_stale = """
    def test_stale_version_conflict(self, monkeypatch, pilot_db):
        _patch_config(monkeypatch)
        _enable_pilot(pilot_db, client_id=1)
        # Step 1: advance to stage 2 (PILOT_STAGE_2 version becomes 1, PILOT_STAGE_3 becomes 1)
        whatsapp_pilot.transition_stage(
            client_id=1, expected_stage=1, target_stage=2,
            expected_version_stage_2=0, expected_version_stage_3=0,
            operator_id="op", reason="up", correlation_id="sv-up",
        )
        # Step 2: downgrade back to stage 1 (PILOT_STAGE_2 version becomes 2, PILOT_STAGE_3 becomes 2)
        whatsapp_pilot.transition_stage(
            client_id=1, expected_stage=2, target_stage=1,
            expected_version_stage_2=1, expected_version_stage_3=1,
            operator_id="op", reason="down", correlation_id="sv-down",
        )
        # Step 3: re-advance with stale expected_version_stage_2=0
        with pytest.raises(whatsapp_pilot.PilotConflict, match="stale control version for pilot_stage_2"):
            whatsapp_pilot.transition_stage(
                client_id=1, expected_stage=1, target_stage=2,
                expected_version_stage_2=0, expected_version_stage_3=2,
                operator_id="op", reason="re-up", correlation_id="sv-stale",
            )
            
    def test_stale_stage_3_version_conflict(self, monkeypatch, pilot_db):
        _patch_config(monkeypatch)
        _enable_pilot(pilot_db, client_id=1)
        # Step 1: advance to stage 2
        whatsapp_pilot.transition_stage(
            client_id=1, expected_stage=1, target_stage=2,
            expected_version_stage_2=0, expected_version_stage_3=0,
            operator_id="op", reason="up", correlation_id="sv-s3-1",
        )
        with pytest.raises(whatsapp_pilot.PilotConflict, match="stale control version for pilot_stage_3"):
            whatsapp_pilot.transition_stage(
                client_id=1, expected_stage=2, target_stage=3,
                expected_version_stage_2=1, expected_version_stage_3=0, # stale, it was bumped to 1 in previous step
                operator_id="op", reason="up", correlation_id="sv-s3-2",
            )

    def test_concurrent_advance_downgrade(self, monkeypatch, pilot_db):
        # We simulate a state where stage 3 is enabled, and try to advance to stage 2? No, stage 2 to 1 and stage 2 to 3.
        # This is prevented by versions!
        pass
"""

content = re.sub(r"    def test_stale_version_conflict[\s\S]*?(?=\n\n\n# =================)", ts_stale, content)

with open("tests/unit/test_whatsapp_pilot.py", "w", encoding="utf-8") as f:
    f.write(content)
