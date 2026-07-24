import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from dotenv import load_dotenv
load_dotenv()
import logging
logging.basicConfig(level=logging.INFO)
from main import calendly_sync_job
from app.clients.airtable_client import AirtableClient

a = AirtableClient()

# Print Airtable record BEFORE (using new dummy phone)
print("=== AIRTABLE RECORD BEFORE ===")
lead_before = a.get_lead('919999900001')
if lead_before:
    f = lead_before.get('fields', {})
    print(f"  Phone  : {f.get('Phone number type')}")
    print(f"  Status : {f.get('Status')}")
    print(f"  Score  : {f.get('Lead_Score')}")
else:
    print("  Lead NOT found under 919999900001")

print("\n=== TRIGGERING CALENDLY SYNC ===")
calendly_sync_job()

# Print Airtable record AFTER
print("\n=== AIRTABLE RECORD AFTER ===")
lead_after = a.get_lead('919999900001')
if lead_after:
    f = lead_after.get('fields', {})
    print(f"  Phone  : {f.get('Phone number type')}")
    print(f"  Status : {f.get('Status')}")
    print(f"  Score  : {f.get('Lead_Score')}")
else:
    print("  Lead NOT found under 919999900001")
