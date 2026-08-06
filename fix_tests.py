import re

with open("tests/unit/test_whatsapp_pilot.py", "r", encoding="utf-8") as f:
    content = f.read()

# Fix whatsapp_operations.mutate calls (like in _enable_pilot)
content = re.sub(
    r"whatsapp_operations\.mutate\([\s\S]*?\)",
    lambda m: m.group(0).replace("expected_version_stage_2", "expected_version").replace(", expected_version_stage_3=0", ""),
    content
)

# Fix whatsapp_pilot.set_enabled calls
content = re.sub(
    r"whatsapp_pilot\.set_enabled\([\s\S]*?\)",
    lambda m: m.group(0).replace("expected_version_stage_2", "expected_version").replace(", expected_version_stage_3=0", ""),
    content
)

with open("tests/unit/test_whatsapp_pilot.py", "w", encoding="utf-8") as f:
    f.write(content)
