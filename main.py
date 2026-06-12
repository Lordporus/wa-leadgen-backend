from fastapi import FastAPI, Request, HTTPException
from pydantic import BaseModel
import logging
from config import WHATSAPP_VERIFY_TOKEN
from whatsapp_client import WhatsAppClient
from gemini_client import GeminiClient
from airtable_client import AirtableClient

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="WhatsApp Acquisition Backend")

whatsapp = WhatsAppClient()
gemini = GeminiClient()
airtable = AirtableClient()

@app.get("/")
def read_root():
    return {"status": "ok", "message": "WhatsApp Acquisition System is running."}

@app.get("/webhook")
def verify_webhook(request: Request):
    """
    Meta Webhook Verification Route.
    Meta sends a GET request here when you configure the webhook in the App Dashboard.
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
    body = await request.json()
    
    # Process only if it's a valid WhatsApp API payload
    if body.get("object") == "whatsapp_business_account":
        for entry in body.get("entry", []):
            for change in entry.get("changes", []):
                value = change.get("value", {})
                if "messages" in value:
                    for message in value["messages"]:
                        sender_phone = message.get("from")
                        message_type = message.get("type")
                        
                        if message_type == "text":
                            user_text = message["text"]["body"]
                            logger.info(f"Received message from {sender_phone}: {user_text}")
                            
                            # 0. Check if lead exists, create if not
                            if not airtable.get_lead(sender_phone):
                                logger.info(f"New inbound lead detected: {sender_phone}")
                                airtable.add_lead(name="WhatsApp User", phone=sender_phone, source="WhatsApp Inbound")
                                
                            # 1. Log incoming message to Airtable (overwrite Last_Message)
                            airtable.update_last_message(sender_phone, user_text, "User")
                            
                            # Extract lead info (Name, Business_Name) asynchronously-ish
                            info = gemini.extract_lead_info(user_text)
                            extracted_name = info.get("Name")
                            extracted_biz = info.get("Business_Name")
                            if extracted_name or extracted_biz:
                                airtable.update_lead_info(sender_phone, extracted_name, extracted_biz)
                            
                            # 2. Update status to 'Contacted' if they reply
                            airtable.update_lead_status(sender_phone, "Contacted")
                            
                            # 3. Generate AI response
                            ai_response = gemini.generate_response(sender_phone, user_text)
                            
                            # 4. Send the response back via WhatsApp
                            whatsapp.send_message(sender_phone, ai_response)
                            
                            # 5. Log outbound message to Airtable (overwrite Last_Message)
                            airtable.update_last_message(sender_phone, ai_response, "AI")
                            
                            # 6. Evaluate intent for status updates
                            lower_resp = ai_response.lower()
                            if any(word in lower_resp for word in ["book", "appointment", "confirm", "time"]):
                                airtable.update_lead_status(sender_phone, "Booked")
                            elif any(word in lower_resp for word in ["call", "connect", "team", "sure"]):
                                airtable.update_lead_status(sender_phone, "Qualified")
                            
        return {"status": "success"}
    return {"status": "ignored"}
