import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import os
import time
import asyncio
from dotenv import load_dotenv

load_dotenv()
os.environ['MIGRATION_MODE'] = 'dual'
os.environ['WHATSAPP_SIMULATE_HUMAN_DELAY'] = 'false'

from app.store.store import get_primary_store, get_secondary_store
from app.store.webhook_store import WebhookStore
from app.core.database import init_engine
from app.core.config import DATABASE_URL
from app.clients.whatsapp_client import WhatsAppClient
from app.clients.gemini_client import GeminiClient
from app.services import tenant

class MockBackgroundTasks:
    def __init__(self):
        self.tasks = []
    
    def add_task(self, func, *args, **kwargs):
        self.tasks.append((func, args, kwargs))

init_engine(DATABASE_URL)
bg = MockBackgroundTasks()
store = WebhookStore(get_primary_store(), get_secondary_store(), bg)
wa = WhatsAppClient()
gemini = GeminiClient()

phone = "9999999881"
name = "Antigravity Test 2"

print(f"Store configured as: {type(store).__name__}")

async def main():
    print("Warming up database connection pool...")
    store.get_lead("000")  # Dummy query to establish TLS handshake
    
    t_start = time.time()
    
    # 1. Lead lookup
    t0 = time.time()
    lead = store.get_lead(phone)
    t1 = time.time()
    print(f"Lead lookup took: {t1-t0:.2f}s (Lead found: {bool(lead)})")
    
    # 2. Add lead
    if not lead:
        t0 = time.time()
        lead = store.add_lead(name, phone, "WhatsApp Inbound")
        t1 = time.time()
        print(f"Add lead took: {t1-t0:.2f}s")
    
    # 3. Append message (Inbound)
    t0 = time.time()
    store.append_message(phone, "INBOUND", "Test message", "text", "wamid.test_123")
    t1 = time.time()
    print(f"Append inbound message took: {t1-t0:.2f}s")
    
    # 4. Gemini reply
    t0 = time.time()
    parsed_history = gemini.parse_conversation_history("Test message")
    reply = gemini.generate_response_with_history(parsed_history, "Test message")
    t1 = time.time()
    print(f"Gemini reply took: {t1-t0:.2f}s")
    
    # 5. Send WhatsApp message
    t0 = time.time()
    res = wa.send_message(phone, reply)
    t1 = time.time()
    print(f"WhatsApp send took: {t1-t0:.2f}s")
    print(f"WhatsApp response: {res}")
    
    # 6. Append message (Outbound)
    t0 = time.time()
    store.append_message(phone, "OUTBOUND", reply, "text", res if res else "failed")
    if not res:
        store.update_message_status("failed", "failed")
    t1 = time.time()
    print(f"Append outbound message took: {t1-t0:.2f}s")
    
    t_end = time.time()
    print(f"\n--- Total Webhook Synchronous Latency: {t_end-t_start:.2f}s ---")
    print(f"Background tasks queued: {len(bg.tasks)}")
    
    # 7. Execute background tasks sequentially to simulate what happens after 200 OK
    print("\n--- Executing Background Tasks ---")
    t_bg_start = time.time()
    for func, args, kwargs in bg.tasks:
        try:
            func(*args, **kwargs)
        except Exception as e:
            print(f"BG Error: {e}")
    t_bg_end = time.time()
    print(f"Background execution took: {t_bg_end-t_bg_start:.2f}s")

if __name__ == "__main__":
    asyncio.run(main())
