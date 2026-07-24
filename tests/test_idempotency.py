import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import os
import sys
import uuid
from unittest.mock import patch
from fastapi.testclient import TestClient

# Mock env vars before importing main
os.environ["DATABASE_URL"] = os.environ.get("DATABASE_URL", "")
os.environ["WHATSAPP_VERIFY_TOKEN"] = "test_token"
os.environ["GEMINI_API_KEY"] = "test"
os.environ["WHATSAPP_ACCESS_TOKEN"] = "test"
os.environ["WHATSAPP_PHONE_NUMBER_ID"] = "test"
os.environ["MIGRATION_MODE"] = "postgres" # ensure only DB is used for test

sys.path.append(os.path.dirname(os.path.abspath(__file__)))
from main import app
from app.core.database import SessionLocal
from app.core.models import Message, Lead

client = TestClient(app)

def make_payload(wamid: str, phone: str = "919999999999"):
    return {
        'object': 'whatsapp_business_account',
        'entry': [{
            'id': '123',
            'changes': [{
                'value': {
                    'messaging_product': 'whatsapp',
                    'metadata': {'display_phone_number': '123', 'phone_number_id': '123'},
                    'contacts': [{'profile': {'name': 'Idempotency Test'}, 'wa_id': phone}],
                    'messages': [{
                        'from': phone,
                        'id': wamid,
                        'timestamp': '1610000000',
                        'text': {'body': 'Idempotency Test Message'},
                        'type': 'text'
                    }]
                },
                'field': 'messages'
            }]
        }]
    }

def get_message_count(phone: str):
    db = SessionLocal()
    try:
        lead = db.query(Lead).filter(Lead.phone == phone).first()
        if not lead:
            return 0
        return db.query(Message).filter(Message.lead_id == lead.id).count()
    finally:
        db.close()

def test_idempotency_same_webhook_twice():
    phone = f"919999{uuid.uuid4().hex[:6]}"
    wamid = f"wamid.{uuid.uuid4().hex}"
    payload = make_payload(wamid, phone)
    
    with patch("gemini_client.GeminiClient.generate_response_with_history", return_value="AI Reply") as mock_gemini:
        with patch("whatsapp_client.WhatsAppClient.send_message", return_value=f"wamid.out.{uuid.uuid4().hex}") as mock_wa:
            with patch("main.verify_signature", return_value=True):
                
                # Send first time
                response1 = client.post("/webhook", json=payload)
                assert response1.status_code == 200
                
                # Verify called once
                assert mock_gemini.call_count == 1
                assert mock_wa.call_count == 1
                
                count_after_first = get_message_count(phone)
                assert count_after_first == 2 # 1 inbound, 1 outbound
                
                # Send second time (duplicate)
                response2 = client.post("/webhook", json=payload)
                assert response2.status_code == 200 # Must return 200 OK immediately
                
                # Verify NOT called again
                assert mock_gemini.call_count == 1
                assert mock_wa.call_count == 1
                
                count_after_second = get_message_count(phone)
                assert count_after_second == 2 # Still 2!
            
def test_idempotency_five_retries():
    phone = f"919999{uuid.uuid4().hex[:6]}"
    wamid = f"wamid.{uuid.uuid4().hex}"
    payload = make_payload(wamid, phone)
    
    with patch("gemini_client.GeminiClient.generate_response_with_history", return_value="AI Reply") as mock_gemini:
        with patch("whatsapp_client.WhatsAppClient.send_message", return_value=f"wamid.out.{uuid.uuid4().hex}") as mock_wa:
            with patch("main.verify_signature", return_value=True):
                # 5 identical retries
                for _ in range(5):
                    res = client.post("/webhook", json=payload)
                    assert res.status_code == 200
                
            assert mock_gemini.call_count == 1
            assert mock_wa.call_count == 1
            assert get_message_count(phone) == 2

if __name__ == "__main__":
    print("Running idempotency tests...")
    test_idempotency_same_webhook_twice()
    print("test_idempotency_same_webhook_twice PASSED")
    test_idempotency_five_retries()
    print("test_idempotency_five_retries PASSED")
    print("All idempotency tests passed successfully!")
