with open("tests/unit/test_whatsapp_pilot.py", "r", encoding="utf-8") as f:
    content = f.read()

content = content.replace(r'\"', '"')

with open("tests/unit/test_whatsapp_pilot.py", "w", encoding="utf-8") as f:
    f.write(content)
