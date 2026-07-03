import subprocess
import time
import requests

print("Starting uvicorn...")
p = subprocess.Popen(['uvicorn', 'main:app', '--host', '127.0.0.1', '--port', '8000'])
time.sleep(5)

try:
    print("Testing GET /")
    r = requests.get('http://127.0.0.1:8000/')
    print(f"Status: {r.status_code}")
    print(f"Headers: {r.headers}")
    print(f"Body: {r.text}")
    
    print("Triggering rate limit on GET /")
    hit_429 = False
    for _ in range(65):
        r = requests.get('http://127.0.0.1:8000/')
        if r.status_code == 429:
            print(f"Status: {r.status_code}")
            print(f"Headers: {r.headers}")
            hit_429 = True
            break
    if not hit_429:
        print("Failed to hit rate limit")
        
    print("Testing GET /webhook")
    r = requests.get('http://127.0.0.1:8000/webhook')
    print(f"Status: {r.status_code}")
    
finally:
    p.terminate()
    print("Uvicorn stopped.")
