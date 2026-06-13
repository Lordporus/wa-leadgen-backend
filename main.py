from fastapi import FastAPI, Request, HTTPException
import logging
import hashlib
import hmac
import os
from datetime import datetime, timedelta
from contextlib import asynccontextmanager
from apscheduler.schedulers.background import BackgroundScheduler
from config import WHATSAPP_VERIFY_TOKEN, WHATSAPP_APP_SECRET, LORD_PHONE_NUMBER
from whatsapp_client import WhatsAppClient
from airtable_client import AirtableClient
from gemini_client import GeminiClient
from calendly_client import CalendlyClient

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

whatsapp = WhatsAppClient()
airtable = AirtableClient()
gemini = GeminiClient()
calendly = CalendlyClient()

def follow_up_job():
    logger.info("Running hourly follow-up job...")
    records = airtable._search("{Status}='Contacted'")
    now = datetime.now()
    for r in records:
        last_msg = r.get("fields", {}).get("Last_Message", "")
        if last_msg:
            try:
                last_line = last_msg.strip().split('\n')[-1]
                time_str = last_line.split(']')[0].strip('[')
                msg_time = datetime.strptime(time_str, "%Y-%m-%d %H:%M:%S")
                if now - msg_time > timedelta(hours=48):
                    phone = r.get("fields", {}).get("Phone number type")
                    logger.info(f"[DRY-RUN] Lead {phone} is eligible for follow-up (Contacted > 48h). Template dentist_followup_v1 pending approval.")
            except Exception as e:
                logger.error(f"Error parsing timestamp for follow-up: {e}")

def calendly_sync_job():
    logger.info("Running Calendly sync job...")
    bookings = calendly.get_recent_bookings()
    if not bookings:
        logger.info("No recent Calendly bookings found.")
        return
        
    for booking in bookings:
        phone = booking.get("phone")
        if not phone:
            logger.info(f"Unmatched booking (no phone provided): {booking.get('name')}")
            continue
            
        lead = airtable.get_lead(phone)
        if lead:
            current_status = lead.get("fields", {}).get("Status")
            if current_status == "Qualified":
                airtable.update_lead_status(phone, "Booked")
                airtable.append_message(phone, "system", f"Calendly Booking Confirmed for {booking.get('start_time')}", "system")
                if LORD_PHONE_NUMBER:
                    whatsapp.send_message(LORD_PHONE_NUMBER, f"📅 BOOKED: {booking.get('name')} booked a call for {booking.get('start_time')}")
            else:
                logger.info(f"Matched booking for {phone} but lead status is {current_status}, not Qualified.")
        else:
            logger.info(f"Unmatched booking (phone {phone} not in Leads): {booking.get('name')}")

scheduler = BackgroundScheduler()
scheduler.add_job(follow_up_job, 'interval', hours=1)
scheduler.add_job(calendly_sync_job, 'interval', hours=1)

@asynccontextmanager
async def lifespan(app: FastAPI):
    scheduler.start()
    yield
    scheduler.shutdown()

app = FastAPI(title="WhatsApp Acquisition Backend", lifespan=lifespan)

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
                                if LORD_PHONE_NUMBER:
                                    whatsapp.send_message(LORD_PHONE_NUMBER, f"🔥 HOT LEAD ALERT: Check Airtable for {lead.get('fields', {}).get('Name', 'Unknown')} ({sender_phone})")
                                else:
                                    logger.info(f"🔥 HOT LEAD: {lead.get('fields', {}).get('Name', 'Unknown')} {sender_phone}")
                            elif score == "Cold":
                                decline_keywords = ["not interested", "stop", "no", "nahi", "cancel", "unsubscribe"]
                                if any(word in user_text.lower() for word in decline_keywords):
                                    airtable.update_lead_status(sender_phone, "Lost")
                                    logger.info(f"Lead {sender_phone} marked as Lost due to explicit decline.")
                            
                # Check for message status updates (delivered/read)
                elif "statuses" in value:
                    for status in value["statuses"]:
                        logger.info(f"Message {status['id']} to {status['recipient_id']} status: {status['status']}")
                            
        return {"status": "success"}
    return {"status": "ignored"}
