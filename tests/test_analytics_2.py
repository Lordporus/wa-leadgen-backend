import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import os
from dotenv import load_dotenv

load_dotenv()
os.environ['MIGRATION_MODE'] = 'dual'

from app.store.store import get_store

store = get_store()
print(f"Store configured as: {type(store).__name__}")

lead = store.get_lead("9999999881")
if not lead:
    print("Lead not found.")
else:
    print(f"Lead ID: {lead['id']}, Current Status: {lead['fields']['Status']}")
    new_status = "Qualified" if lead['fields']['Status'] != "Qualified" else "Contacted"
    print(f"Updating status to: {new_status}")
    
    updated = store.update_lead_status_by_id(lead['id'], new_status)
    if updated:
        print(f"Success! New status in DB: {updated['status']}")
    else:
        print("Update failed.")
