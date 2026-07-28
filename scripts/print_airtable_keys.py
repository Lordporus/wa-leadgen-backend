import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import asyncio
from app.store.store import get_store
from app.core.config import CLIENT_ID

async def main():
    # fetch raw records
    formula = ""
    store = get_store()
    resp = store._search(formula, client_id=CLIENT_ID)
    if resp:
        print("Raw Airtable Fields:")
        print(list(resp[0].get("fields", {}).keys()))
    else:
        print("No records found")

asyncio.run(main())
