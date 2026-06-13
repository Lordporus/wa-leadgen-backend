from fastapi import FastAPI, Request, HTTPException
import logging
import hashlib
import hmac
from config import WHATSAPP_VERIFY_TOKEN, WHATSAPP_APP_SECRET
from whatsapp_client import WhatsAppClient
from airtable_client import AirtableClient
from gemini_client import GeminiClient

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="WhatsApp Acquisition Backend")

whatsapp = WhatsAppClient()
airtable = AirtableClient()
gemini = GeminiClient()

def verify_signature(payload: bytes, signature_header: str) -> bool:
    """Verify Meta's X-Hub-Signature-256 header."""
    if not WHATSAPP_APP_SECRET or not signature_header:
        return False
    expected_sig = hmac.new(
        WHATSAPP_APP_SECRET.encode('utf-8'),
        msg=payload,
        digestmod=hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(f"sha256={expected_sig}", signature_header)


@app.get("/")
def read_root():
    return {"status": "ok", "message": "WhatsApp Acquisition System is running."}

@app.get("/webhook")
def verify_webhook(request: Request):
    """
    Meta Webhook Verification Route.
    """
    mode = request.query_params.get("hub.mode")
    token = request.query_params.get("hub.verify_token")
    challenge = request.query_params.get("hub.challenge")

    if mode and token:
        if mode == "subscribe" and token == WHATSAPP_VERIFY_TOKEN:
            logger.info("Webhook verified successfully.")
            return int(challenge)
        else:
            raise HTTPException(status_code=403, detail="Verification token mismatch")
    
    raise HTTPException(status_code=400, detail="Bad Request")

@app.post("/webhook")
async def receive_message(request: Request):
    """
    Receive incoming messages from WhatsApp users.
    """
    # 1. Verify signature
    signature = request.headers.get("X-Hub-Signature-256")
    body_bytes = await request.body()
    if WHATSAPP_APP_SECRET and not verify_signature(body_bytes, signature):
        logger.warning("Invalid webhook signature rejected.")
        raise HTTPException(status_code=403, detail="Invalid signature")

    body = await request.json()
    
    if body.get("object") == "whatsapp_business_account":
        for entry in body.get("entry", []):
            for change in entry.get("changes", []):
                value = change.get("value", {})
                
                # Check for incoming messages
                if "messages" in value:
                    for message in value["messages"]:
                        sender_phone = message.get("from")
                        message_type = message.get("type")
                        
                        if message_type == "text":
                            user_text = message["text"]["body"]
                            logger.info(f"Received message from {sender_phone}: {user_text}")
                            
                            lead = airtable.get_lead(sender_phone)
                            if not lead:
                                logger.info(f"Message from unknown number {sender_phone}. Logging and ignoring.")
                                continue
                                
                            # If matched: log message
                            airtable.append_message(sender_phone, direction="inbound", message=user_text, msg_type="text")
                            
                            # Refresh lead to get updated Last_Message
                            lead = airtable.get_lead(sender_phone)
                            current_status = lead.get("fields", {}).get("Status")
                            
                            # Update lead status to "Contacted" if currently "New Lead"
                            if current_status == "New Lead":
                                airtable.update_lead_status(sender_phone, "Contacted")
                                
                            # Phase 4: AI Routing & Scoring
                            last_message = lead.get("fields", {}).get("Last_Message", "")
                            parsed_history = gemini.parse_conversation_history(last_message)
                            
                            ai_reply = gemini.generate_response_with_history(parsed_history, user_text)
                            whatsapp.send_message(sender_phone, ai_reply)
                            airtable.append_message(sender_phone, direction="outbound", message=ai_reply, msg_type="text")
                            
                            # Refresh lead to score the full conversation including the outbound message
                            lead_after_reply = airtable.get_lead(sender_phone)
                            updated_last_message = lead_after_reply.get("fields", {}).get("Last_Message", "")
                            
                            score = gemini.score_lead(updated_last_message)
                            airtable.update_lead_score(sender_phone, score)
                            
                            if score == "Hot":
                                airtable.update_lead_status(sender_phone, "Qualified")
                                logger.info(f"🔥 HOT LEAD: {lead.get('fields', {}).get('Name', 'Unknown')} {sender_phone}")
                            
                # Check for message status updates (delivered/read)
                elif "statuses" in value:
                    for status in value["statuses"]:
                        logger.info(f"Message {status['id']} to {status['recipient_id']} status: {status['status']}")
                            
        return {"status": "success"}
    return {"status": "ignored"}
