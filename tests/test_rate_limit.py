import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import time
from fastapi.testclient import TestClient
from main import app

client = TestClient(app, raise_server_exceptions=False)

def test_public_endpoint():
    print("Testing GET /webhook (Limit: 10/minute)...")
    
    # 1. Below limit (5 requests)
    for _ in range(5):
        resp = client.get("/webhook")
        assert resp.status_code != 429, f"Unexpected 429: {resp.text}"

    # 2. Hit the limit (next 5 requests)
    for _ in range(5):
        resp = client.get("/webhook")
        
    # 3. Above limit (11th request)
    resp = client.get("/webhook")
    assert resp.status_code == 429, f"Expected 429, got {resp.status_code}"
    
    assert "retry-after" in resp.headers, "Missing Retry-After header"
    print(f"✅ GET /webhook correctly returned 429. Retry-After: {resp.headers['retry-after']}")

def test_admin_endpoint():
    print("Testing POST /api/admin/clients (Limit: 10/minute)...")
    headers = {"X-Admin-Secret": "fake_test_secret"}
    payload = {"name": "test", "wa_phone_number_id": "123"}
    
    # Send 10 requests (below/at limit)
    for _ in range(10):
        resp = client.post("/api/admin/clients", headers=headers, json=payload)
        assert resp.status_code != 429
        
    # 11th request (above limit)
    resp = client.post("/api/admin/clients", headers=headers, json=payload)
    assert resp.status_code == 429
    print(f"✅ Admin endpoint correctly rate limited. Retry-After: {resp.headers['retry-after']}")
    
def test_webhook_post():
    print("Testing POST /webhook (Limit: 1000/minute)...")
    # Verify no regression
    for _ in range(5):
        resp = client.post("/webhook", json={"object": "whatsapp_business_account"})
        # Should be 403 due to invalid signature, but not 429.
        assert resp.status_code != 429
    print("✅ Meta webhook (POST) succeeds under normal load.")

if __name__ == "__main__":
    test_public_endpoint()
    test_admin_endpoint()
    test_webhook_post()
    print("All automated verifications passed!")
