import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import asyncio
import httpx
import os
import sys
import uuid
import time
from datetime import datetime

BASE_URL = os.getenv("PROD_URL", "https://whatsapp-acquisition-backend.onrender.com")
API_KEY = os.getenv("API_KEY")
ADMIN_SECRET = os.getenv("ADMIN_SECRET")

# Metrics
metrics = {
    "200": 0,
    "403": 0,
    "429": 0,
    "other": 0,
    "response_times": []
}

def record_metric(resp: httpx.Response, start_time: float):
    elapsed = time.time() - start_time
    metrics["response_times"].append(elapsed)
    status = str(resp.status_code)
    if status in metrics:
        metrics[status] += 1
    else:
        metrics["other"] += 1

async def make_request(client, method, url, headers=None, json=None):
    if headers is None:
        headers = {}
    
    start_time = time.time()
    try:
        if method.upper() == "GET":
            resp = await client.get(url, headers=headers)
        else:
            resp = await client.post(url, headers=headers, json=json)
        record_metric(resp, start_time)
        return resp
    except Exception as e:
        print(f"Error making request: {e}")
        return None

async def verify_endpoint(name, method, path, limit, headers=None, json=None, expect_status=200):
    print(f"\n--- Verifying {name} ({method} {path}) ---")
    ip = str(uuid.uuid4()) # spoof unique IP
    client_headers = headers.copy() if headers else {}
    client_headers["X-Forwarded-For"] = ip
    
    async with httpx.AsyncClient(base_url=BASE_URL) as client:
        # Below limit
        print(f"Testing below limit (sending {limit//2} requests)...", flush=True)
        tasks = [make_request(client, method, path, headers=client_headers, json=json) for _ in range(limit // 2)]
        results = await asyncio.gather(*tasks)
            
        if any(r is None or r.status_code != expect_status for r in results):
            print(f"❌ FAILED: Not all requests below limit succeeded. Expected {expect_status}.", flush=True)
            for r in results:
                if r and r.status_code != expect_status:
                    print(f"   Got {r.status_code}: {r.text}", flush=True)
            return False
        print(f"✅ Below limit passed (all {expect_status}).", flush=True)
        
        # Above limit
        print(f"Testing above limit (sending {limit + 5} requests)...", flush=True)
        tasks = [make_request(client, method, path, headers=client_headers, json=json) for _ in range(limit + 5)]
        results = await asyncio.gather(*tasks)
            
        has_429 = False
        retry_after_ok = False
        for r in results:
            if r and r.status_code == 429:
                has_429 = True
                if "retry-after" in r.headers:
                    retry_after_ok = True
                    break
                    
        if not has_429:
            print(f"❌ FAILED: Did not receive 429 above limit.")
            return False
        if not retry_after_ok:
            print(f"❌ FAILED: Received 429 but missing Retry-After header.")
            return False
            
        print("✅ Above limit passed (received 429 with Retry-After).")
        return True


async def verify_webhook_post():
    print("\n--- Verifying POST /webhook (Meta Webhook) ---")
    limit = 1000
    ip = str(uuid.uuid4())
    headers = {"X-Forwarded-For": ip}
    json_payload = {"object": "whatsapp_business_account"} # fake Meta payload
    
    async with httpx.AsyncClient(base_url=BASE_URL) as client:
        print("Testing normal Meta webhook...")
        resp = await make_request(client, "POST", "/webhook", headers=headers, json=json_payload)
        # We expect 403 because signature is invalid, but it shouldn't be 429
        if resp.status_code == 429:
            print("❌ FAILED: Normal webhook was rate limited immediately.")
            return False
        print("✅ Normal webhook succeeds (not blocked by rate limit).")
        
        print("Testing burst traffic below limit (50 requests)...", flush=True)
        tasks = [make_request(client, "POST", "/webhook", headers=headers, json=json_payload) for _ in range(50)]
        results = await asyncio.gather(*tasks)
        if any(r and r.status_code == 429 for r in results):
             print("❌ FAILED: Burst traffic was rate limited falsely.", flush=True)
             return False
        print("✅ Burst traffic below limit passed.", flush=True)
        
        print(f"Testing above configured limit ({limit + 5} requests)...", flush=True)
        tasks = [make_request(client, "POST", "/webhook", headers=headers, json=json_payload) for _ in range(limit + 5)]
        results = await asyncio.gather(*tasks)
        has_429 = any(r and r.status_code == 429 for r in results)
        
        if not has_429:
            print("❌ FAILED: Did not receive 429 above limit for webhook.", flush=True)
            return False
        print("✅ Above limit passed (received 429).", flush=True)
        return True

async def verify_auth_endpoints():
    print("\n--- Verifying Authenticated Endpoints (/api/leads) ---", flush=True)
    path = "/api/leads"
    limit = 120
    
    async with httpx.AsyncClient(base_url=BASE_URL) as client:
        print("Testing empty API key...", flush=True)
        ip = str(uuid.uuid4())
        resp = await make_request(client, "GET", path, headers={"X-Forwarded-For": ip})
        if resp.status_code != 403:
             print(f"❌ FAILED: Empty API key returned {resp.status_code} instead of 403.", flush=True)
             return False
        print("✅ Empty API key correctly blocked (403).", flush=True)
        
        print("Testing invalid API key...", flush=True)
        resp = await make_request(client, "GET", path, headers={"X-API-Key": "invalid_key_123", "X-Forwarded-For": ip})
        if resp.status_code != 403:
             print(f"❌ FAILED: Invalid API key returned {resp.status_code} instead of 403.", flush=True)
             return False
        print("✅ Invalid API key correctly blocked (403).", flush=True)
        
        if not API_KEY:
            print("⚠️ Skipping valid API key tests (API_KEY env var not set).", flush=True)
        else:
            print("Testing valid API key under limit...", flush=True)
            ip = str(uuid.uuid4())
            headers = {"X-API-Key": API_KEY, "X-Forwarded-For": ip}
            
            # Send 5 requests (below limit)
            tasks = [make_request(client, "GET", path, headers=headers) for _ in range(5)]
            results = await asyncio.gather(*tasks)
            if any(r.status_code != 200 for r in results):
                print("❌ FAILED: Valid API key under limit did not return 200.", flush=True)
                return False
            print("✅ Valid API key under limit passed (200).", flush=True)
            
            print(f"Testing valid API key above limit ({limit + 5} requests)...", flush=True)
            tasks = [make_request(client, "GET", path, headers=headers) for _ in range(limit + 5)]
            results = await asyncio.gather(*tasks)
            if not any(r.status_code == 429 for r in results):
                print("❌ FAILED: Valid API key above limit did not return 429.", flush=True)
                return False
            print("✅ Valid API key above limit passed (429).", flush=True)
            
    return True

async def verify_admin_endpoint():
    print("\n--- Verifying Admin Endpoint (/api/admin/clients) ---")
    path = "/api/admin/clients"
    limit = 10
    json_payload = {"name": "test", "wa_phone_number_id": "123"}
    
    if not ADMIN_SECRET:
         print("⚠️ No ADMIN_SECRET provided. Testing with invalid secret (Expect 403).")
         secret = "invalid_secret"
         expect = 401
    else:
         secret = ADMIN_SECRET
         expect = 200
         
    headers = {"X-Admin-Secret": secret}
    return await verify_endpoint("POST Admin", "POST", path, limit, headers=headers, json=json_payload, expect_status=expect)


async def main():
    print(f"Starting Production Verification against {BASE_URL}", flush=True)
    print(f"Time: {datetime.now()}", flush=True)
    
    tasks = [
        verify_endpoint("GET Root", "GET", "/", 60, expect_status=200),
        verify_endpoint("GET Webhook", "GET", "/webhook", 10, expect_status=400),
        verify_webhook_post(),
        verify_auth_endpoints(),
        verify_admin_endpoint()
    ]
    
    results = []
    for task in tasks:
        results.append(await task)
        
    print("\n=== Verification Summary ===", flush=True)
    
    if len(metrics["response_times"]) > 0:
        avg_time = sum(metrics["response_times"]) / len(metrics["response_times"])
        print(f"Average response time: {avg_time:.3f} seconds", flush=True)
        
    total_reqs = len(metrics["response_times"])
    print(f"Total requests sent:   {total_reqs}", flush=True)
    print(f"200 Responses:         {metrics['200']}", flush=True)
    print(f"403 Responses:         {metrics['403']}", flush=True)
    print(f"429 Responses:         {metrics['429']}", flush=True)
    
    if all(results):
        print("\n🎉 ALL TESTS PASSED! SPRINT A - P2 COMPLETE.", flush=True)
    else:
        print("\n❌ SOME TESTS FAILED. See log above.", flush=True)

if __name__ == "__main__":
    asyncio.run(main())
