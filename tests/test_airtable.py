import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app.clients.airtable_client import AirtableClient
a = AirtableClient()
records = a._search("{Status} != 'New Lead'")
for r in records:
    print(r.get("fields"))
