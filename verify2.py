import httpx
import asyncio
import uuid
import time

BASE_URL = "https://whatsapp-acquisition-backend.onrender.com"
ADMIN_SECRET = ""  # Let it test with invalid if missing

async def make_requests(path, limit, method="GET", headers=None, json=None, burst_size=0):
    async with httpx.AsyncClient(base_url=BASE_URL) as client:
        # below limit
        ip = str(uuid.uuid4())
        req_headers = headers.copy() if headers else {}
        req_headers["X-Forwarded-For"] = ip
        print(f"Testing {method} {path} - below limit")
        tasks = [client.request(method, path, headers=req_headers, json=json) for _ in range(limit // 2)]
        results = await asyncio.gather(*tasks)
        print([r.status_code for r in results])
        
        # burst
        if burst_size > 0:
            print(f"Testing {method} {path} - burst below limit")
            tasks = [client.request(method, path, headers=req_headers, json=json) for _ in range(burst_size)]
            results = await asyncio.gather(*tasks)
            print([r.status_code for r in results])
            
        # above limit
        print(f"Testing {method} {path} - above limit")
        tasks = [client.request(method, path, headers=req_headers, json=json) for _ in range(limit + 5)]
        results = await asyncio.gather(*tasks)
        print([r.status_code for r in results])

async def main():
    print("GET / (limit 60)")
    await make_requests("/", 60)
    
    print("GET /webhook (limit 10)")
    await make_requests("/webhook", 10)
    
    print("POST /webhook (limit 1000)")
    await make_requests("/webhook", 1000, method="POST", json={"object": "page", "entry": []}, burst_size=50)

if __name__ == "__main__":
    asyncio.run(main())
