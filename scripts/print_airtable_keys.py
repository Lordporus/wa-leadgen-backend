import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import asyncio
from app.store.store import store
from app.clients.airtable_client import airtable_client

async def main():
    # fetch raw records
    formula = ""
    resp = airtable_client._search(formula)
    if resp:
        print("Raw Airtable Fields:")
        print(list(resp[0].get("fields", {}).keys()))
    else:
        print("No records found")

asyncio.run(main())
