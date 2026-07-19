import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import time
import requests

BASE_URL = "https://whatsapp-acquisition-backend.onrender.com"

def verify_prod():
    print(f"Testing rate limiting on {BASE_URL}...")
    
    # 1. Exhaust the limit for GET /webhook (10/min)
    print("Testing GET /webhook rate limit (Limit: 10/min)...")
    for _ in range(10):
        resp = requests.get(f"{BASE_URL}/webhook")
        time.sleep(0.1)
        
    resp = requests.get(f"{BASE_URL}/webhook")
    
    if resp.status_code == 429:
        print("✅ Rate limiting is ACTIVE on production.")
        print(f"✅ 429 Response confirmed.")
        print(f"✅ Retry-After header: {resp.headers.get('retry-after')}")
    else:
        print(f"❌ Rate limiting NOT ACTIVE yet. Status Code: {resp.status_code}")
        return False
        
    # 2. Verify Meta Webhook POST doesn't fail falsely under load
    print("Testing POST /webhook (Limit: 1000/min)...")
    for _ in range(5):
        resp = requests.post(f"{BASE_URL}/webhook", json={"object": "test"})
        if resp.status_code == 429:
            print("❌ False Positive! POST /webhook was rate-limited.")
            return False
            
    print("✅ POST /webhook succeeded under load (No false positives).")
    print("✅ 200 responses (or non-429 equivalents) confirmed for valid paths.")
    return True
    
if __name__ == "__main__":
    max_retries = 15
    for attempt in range(max_retries):
        if verify_prod():
            print("Production verification complete!")
            break
        print(f"Retrying in 30 seconds... (Attempt {attempt+1}/{max_retries})")
        time.sleep(30)
    else:
        print("Production verification timed out.")
