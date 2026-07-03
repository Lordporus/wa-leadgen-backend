import os
from dotenv import load_dotenv
import httpx
import sys

load_dotenv()

phone = "9999999888"

print("--- Checking Postgres ---")
db_url = os.getenv('DATABASE_URL')
if not db_url:
    print("No DATABASE_URL found.")
else:
    import psycopg2
    try:
        conn = psycopg2.connect(db_url)
        cur = conn.cursor()
        cur.execute("SELECT id, name, phone, status FROM leads WHERE phone = %s", (phone,))
        row = cur.fetchone()
        if row:
            print("Found in Postgres (Lead):", row)
            cur.execute("SELECT * FROM messages WHERE lead_id = %s", (row[0],))
            msgs = cur.fetchall()
            print("Messages for Lead:", msgs)
        else:
            print("Not found in Postgres.")
        conn.close()
    except Exception as e:
        print("Postgres error:", e)

print("--- Checking Airtable ---")
airtable_key = os.getenv('AIRTABLE_API_KEY')
base_id = os.getenv('AIRTABLE_BASE_ID')
table_name = os.getenv('AIRTABLE_TABLE_NAME')
if airtable_key and base_id and table_name:
    url = f"https://api.airtable.com/v0/{base_id}/{table_name}?filterByFormula=phone='{phone}'"
    headers = {"Authorization": f"Bearer {airtable_key}"}
    try:
        r = httpx.get(url, headers=headers)
        data = r.json()
        records = data.get('records', [])
        if records:
            print("Found in Airtable:", records[0]['fields'])
        else:
            print("Not found in Airtable.")
    except Exception as e:
        print("Airtable error:", e)
else:
    print("Missing Airtable credentials.")
