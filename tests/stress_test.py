import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import json
import hmac
import hashlib
import requests
import time
import os
import statistics
from dotenv import load_dotenv

load_dotenv()

app_secret = os.getenv("WHATSAPP_APP_SECRET")
if not app_secret:
    print("WHATSAPP_APP_SECRET not found.")
    exit(1)

url = "https://whatsapp-acquisition-backend.onrender.com/webhook"

latencies = []
success_count = 0
total_requests = 20

print(f"Starting {total_requests} consecutive webhook stress tests against {url}...\n")

for i in range(total_requests):
    new_phone = f"91999999{int(time.time() * 1000) % 10000:04d}"
    
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
                                        "name": f"Stress Test {i+1}"
                                    },
                                    "wa_id": new_phone
                                }
                            ],
                            "messages": [
                                {
                                    "from": new_phone,
                                    "id": f"wamid.{int(time.time()*1000)}",
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
    
    print(f"Request {i+1:02d}/20 - Sending webhook for {new_phone}...", end=" ", flush=True)
    t0 = time.time()
    try:
        response = requests.post(url, data=payload_bytes, headers=headers, timeout=40)
        t1 = time.time()
        
        latency = t1 - t0
        status = response.status_code
        
        if status == 200:
            success_count += 1
            latencies.append(latency)
            print(f"✅ {latency:.2f}s")
        else:
            print(f"❌ Failed ({status}) in {latency:.2f}s: {response.text}")
    except Exception as e:
        print(f"❌ Error: {e}")
        
    # Give the backend a brief moment to avoid totally overwhelming the connection pool
    time.sleep(2)

print("\n--- STRESS TEST RESULTS ---")
print(f"Success Rate: {success_count}/{total_requests} ({(success_count/total_requests)*100:.1f}%)")

if latencies:
    print(f"Average Latency: {statistics.mean(latencies):.2f}s")
    print(f"Min Latency: {min(latencies):.2f}s")
    print(f"Max Latency: {max(latencies):.2f}s")
    
    # Calculate p95
    sorted_latencies = sorted(latencies)
    p95_index = int(len(sorted_latencies) * 0.95) - 1
    if p95_index < 0:
        p95_index = 0
    print(f"p95 Latency: {sorted_latencies[p95_index]:.2f}s")
