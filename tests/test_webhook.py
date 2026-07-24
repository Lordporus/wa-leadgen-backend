import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import json
import httpx

payload = {
    'object': 'whatsapp_business_account',
    'entry': [{
        'id': '123',
        'changes': [{
            'value': {
                'messaging_product': 'whatsapp',
                'metadata': {'display_phone_number': '123', 'phone_number_id': '123'},
                'contacts': [{'profile': {'name': 'Antigravity Test'}, 'wa_id': '9999999999'}],
                'messages': [{
                    'from': '9999999999',
                    'id': 'wamid.HBgLOTE...',
                    'timestamp': '1610000000',
                    'text': {'body': 'Test message for dual write!'},
                    'type': 'text'
                }]
            },
            'field': 'messages'
        }]
    }]
}

try:
    r = httpx.post('http://localhost:8001/webhook', json=payload)
    print('Response:', r.status_code, r.text)
except Exception as e:
    print('Failed to send:', e)
