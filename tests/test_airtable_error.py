import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import os
from dotenv import load_dotenv
import requests
from datetime import datetime

load_dotenv()
AIRTABLE_API_KEY = os.getenv('AIRTABLE_API_KEY')
AIRTABLE_BASE_ID = os.getenv('AIRTABLE_BASE_ID')
AIRTABLE_TABLE_NAME = os.getenv('AIRTABLE_TABLE_NAME')

base_url = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{AIRTABLE_TABLE_NAME}"
headers = {
    "Authorization": f"Bearer {AIRTABLE_API_KEY}",
    "Content-Type": "application/json",
}

fields = {
    "Name": "Unknown",
    "Phone number type": "9999999888",
    "Source": "WhatsApp",
    "Status": "New Lead",
    "Created_At": datetime.now().isoformat(),
}

print("Sending request to Airtable...")
try:
    resp = requests.post(
        base_url,
        headers=headers,
        json={"fields": fields, "typecast": True},
        timeout=10,
    )
    print(f"Status Code: {resp.status_code}")
    print(f"Response: {resp.text}")
    resp.raise_for_status()
except Exception as e:
    print(f"Exception: {e}")
