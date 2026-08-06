import re

with open("tests/unit/test_whatsapp_pilot.py", "r", encoding="utf-8") as f:
    content = f.read()

# Fix _advance_to_stage_3 expected_version_stage_3 to 1
content = re.sub(
    r"whatsapp_operations\.MutationRequest\(control=whatsapp_operations\.PILOT_STAGE_3, enabled_value=True, expected_version=0\)",
    r"whatsapp_operations.MutationRequest(control=whatsapp_operations.PILOT_STAGE_3, enabled_value=True, expected_version=1)",
    content
)

# Fix test_advance_stage_2_to_3
content = re.sub(
    r"def test_advance_stage_2_to_3\([\s\S]*?target_stage=3,\n            expected_version_stage_2=0, expected_version_stage_3=0,",
    r"def test_advance_stage_2_to_3(self, monkeypatch, pilot_db):\n        _patch_config(monkeypatch)\n        _enable_pilot(pilot_db, client_id=1)\n        whatsapp_pilot.transition_stage(\n            client_id=1, expected_stage=1, target_stage=2,\n            expected_version_stage_2=0, expected_version_stage_3=0,\n            operator_id=\"op\", reason=\"up\", correlation_id=\"sv-up\",\n        )\n        result = whatsapp_pilot.transition_stage(\n            client_id=1, expected_stage=2, target_stage=3,\n            expected_version_stage_2=1, expected_version_stage_3=1,",
    content
)

# Fix test_downgrade_stage_3_to_2
content = re.sub(
    r"def test_downgrade_stage_3_to_2\([\s\S]*?target_stage=2,\n            expected_version_stage_2=0, expected_version_stage_3=0,",
    r"def test_downgrade_stage_3_to_2(self, monkeypatch, pilot_db):\n        _patch_config(monkeypatch)\n        _enable_pilot(pilot_db, client_id=1)\n        whatsapp_pilot.transition_stage(\n            client_id=1, expected_stage=1, target_stage=2,\n            expected_version_stage_2=0, expected_version_stage_3=0,\n            operator_id=\"op\", reason=\"up\", correlation_id=\"sv-up\",\n        )\n        whatsapp_pilot.transition_stage(\n            client_id=1, expected_stage=2, target_stage=3,\n            expected_version_stage_2=1, expected_version_stage_3=1,\n            operator_id=\"op\", reason=\"up\", correlation_id=\"sv-up2\",\n        )\n        result = whatsapp_pilot.transition_stage(\n            client_id=1, expected_stage=3, target_stage=2,\n            expected_version_stage_2=2, expected_version_stage_3=2,",
    content
)

with open("tests/unit/test_whatsapp_pilot.py", "w", encoding="utf-8") as f:
    f.write(content)
