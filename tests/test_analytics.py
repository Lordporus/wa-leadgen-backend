import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import os
import json
from fastapi.testclient import TestClient
from main import app

client = TestClient(app)

# The client API key from .env (matches a tenant API key in DB)
api_key = os.getenv("ANALYTICS_API_KEY")
headers = {"X-API-Key": api_key}

print("=== GET /api/analytics/funnel ===")
resp = client.get("/api/analytics/funnel", headers=headers)
print(f"Status: {resp.status_code}")
try:
    print(json.dumps(resp.json(), indent=2))
except Exception as e:
    print(resp.text)

print("\n=== GET /api/analytics/response-time ===")
resp = client.get("/api/analytics/response-time", headers=headers)
print(f"Status: {resp.status_code}")
try:
    print(json.dumps(resp.json(), indent=2))
except Exception as e:
    print(resp.text)

print("\n=== GET /api/analytics/bookings ===")
resp = client.get("/api/analytics/bookings", headers=headers)
print(f"Status: {resp.status_code}")
try:
    print(json.dumps(resp.json(), indent=2))
except Exception as e:
    print(resp.text)
