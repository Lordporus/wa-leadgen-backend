import asyncio
from store import store
from airtable_client import airtable_client

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
