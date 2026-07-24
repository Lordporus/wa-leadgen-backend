import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import json
import httpx
import hmac
import hashlib
import os
from dotenv import load_dotenv

load_dotenv()
secret = os.getenv('WHATSAPP_APP_SECRET', '').encode('utf-8')
phone_id = os.getenv('WHATSAPP_PHONE_NUMBER_ID', '123')

payload = {
    'object': 'whatsapp_business_account',
    'entry': [{
        'id': '123',
        'changes': [{
            'value': {
                'messaging_product': 'whatsapp',
                'metadata': {'display_phone_number': '123', 'phone_number_id': phone_id},
                'contacts': [{'profile': {'name': 'Antigravity Test'}, 'wa_id': '9999999888'}],
                'messages': [{
                    'from': '9999999888',
                    'id': 'wamid.HBgLOTE_test_dual',
                    'timestamp': '1610000000',
                    'text': {'body': 'Test message for dual write from Antigravity!'},
                    'type': 'text'
                }]
            },
            'field': 'messages'
        }]
    }]
}

body = json.dumps(payload).encode('utf-8')
signature = hmac.new(secret, body, hashlib.sha256).hexdigest()
headers = {'X-Hub-Signature-256': f'sha256={signature}', 'Content-Type': 'application/json'}

try:
    r = httpx.post('https://whatsapp-acquisition-backend.onrender.com/webhook', content=body, headers=headers)
    print('Response:', r.status_code, r.text)
except Exception as e:
    print('Failed to send:', e)
