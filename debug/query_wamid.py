import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import os
import requests
from dotenv import load_dotenv

load_dotenv()
token = os.getenv('WHATSAPP_ACCESS_TOKEN')
wamid = 'wamid.HBgMOTE5OTk5OTk5ODg4FQIAERgSRjVEREY1QTc3NENEM0EyRDcxAA=='

url = f"https://graph.facebook.com/v19.0/{wamid}"
headers = {"Authorization": f"Bearer {token}"}
r = requests.get(url, headers=headers)
print(f"Status: {r.status_code}")
print(f"Response: {r.text}")
