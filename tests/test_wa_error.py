import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import logging
from dotenv import load_dotenv
from app.clients.whatsapp_client import WhatsAppClient

logging.basicConfig(level=logging.INFO)
load_dotenv()
client = WhatsAppClient()
res = client.send_message("1", "Test message")
print("WhatsApp Response:")
print(res)
print(res)
