import json
import hmac
import hashlib
import requests
import time
import os
from dotenv import load_dotenv

load_dotenv()

# App secret to sign the payload
app_secret = os.getenv("WHATSAPP_APP_SECRET")
if not app_secret:
    print("WHATSAPP_APP_SECRET not found.")
    exit(1)

# Generate a new phone number
new_phone = f"91999999{int(time.time()) % 10000:04d}"
print(f"Using new phone number: {new_phone}")

# The webhook payload simulating an incoming message
payload_dict = {
    "object": "whatsapp_business_account",
    "entry": [
        {
            "id": "1234567890",
            "changes": [
                {
                    "value": {
                        "messaging_product": "whatsapp",
                        "metadata": {
                            "display_phone_number": "15551234567",
                            "phone_number_id": "1320712694448017"
                        },
                        "contacts": [
                            {
                                "profile": {
                                    "name": "Prod Test User"
                                },
                                "wa_id": new_phone
                            }
                        ],
                        "messages": [
                            {
                                "from": new_phone,
                                "id": f"wamid.{int(time.time())}",
                                "timestamp": str(int(time.time())),
                                "text": {
                                    "body": "Hello, I am interested in building a website."
                                },
                                "type": "text"
                            }
                        ]
                    },
                    "field": "messages"
                }
            ]
        }
    ]
}

payload_bytes = json.dumps(payload_dict).encode("utf-8")
signature = hmac.new(app_secret.encode("utf-8"), payload_bytes, hashlib.sha256).hexdigest()
headers = {
    "Content-Type": "application/json",
    "X-Hub-Signature-256": f"sha256={signature}"
}

url = "https://whatsapp-acquisition-backend.onrender.com/webhook"

print(f"Sending webhook to {url}...")
t0 = time.time()
response = requests.post(url, data=payload_bytes, headers=headers)
t1 = time.time()

print(f"HTTP Response: {response.status_code}")
print(f"Synchronous Webhook Time: {t1 - t0:.2f} seconds")
