import os
import requests
import logging
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)
CALENDLY_API_TOKEN = os.getenv("CALENDLY_API_TOKEN")

class CalendlyClient:
    def __init__(self):
        self.token = CALENDLY_API_TOKEN
        self.headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        self.user_uri = None

    def get_recent_bookings(self):
        if not self.token:
            logger.warning("CALENDLY_API_TOKEN missing. Cannot fetch bookings.")
            return []
            
        try:
            if not self.user_uri:
                user_resp = requests.get("https://api.calendly.com/users/me", headers=self.headers)
                user_resp.raise_for_status()
                self.user_uri = user_resp.json()["resource"]["uri"]
                
            org_uri = None
            if self.user_uri:
                user_resp = requests.get(self.user_uri, headers=self.headers)
                user_resp.raise_for_status()
                org_uri = user_resp.json()["resource"].get("current_organization")
                
            if not org_uri:
                return []

            events_url = f"https://api.calendly.com/scheduled_events?organization={org_uri}&sort=start_time:desc"
            events_resp = requests.get(events_url, headers=self.headers)
            events_resp.raise_for_status()
            events = events_resp.json().get("collection", [])
            
            bookings = []
            now = datetime.now(timezone.utc)
            for event in events:
                # Filter locally to last 2 hours based on created_at
                created_at = datetime.fromisoformat(event["created_at"].replace("Z", "+00:00"))
                if now - created_at > timedelta(hours=2):
                    continue
                    
                invitees_url = f"{event['uri']}/invitees"
                inv_resp = requests.get(invitees_url, headers=self.headers)
                if inv_resp.ok:
                    invitees = inv_resp.json().get("collection", [])
                    for inv in invitees:
                        bookings.append({
                            "name": inv.get("name"),
                            "email": inv.get("email"),
                            "phone": self._extract_phone(inv),
                            "start_time": event.get("start_time")
                        })
            return bookings
        except Exception as e:
            logger.error(f"Calendly sync error: {e}")
            return []

    def _extract_phone(self, invitee: dict):
        import re
        
        # 1. Preferred: SMS reminder number (set when invitee opts into text reminders)
        raw_phone = invitee.get("text_reminder_number")
        
        if not raw_phone:
            for q in invitee.get("questions_and_answers", []):
                answer = q.get("answer", "")
                q_text = q.get("question", "").lower()
                
                # 2. Question explicitly asks for phone/whatsapp
                if "phone" in q_text or "whatsapp" in q_text or "number" in q_text:
                    raw_phone = answer
                    break
                
                # 3. Fallback: any answer that is purely a phone-number-like string
                #    (9+ consecutive digits after stripping +/spaces/dashes)
                digits_only = re.sub(r'[\+\s\-\(\)]', '', answer)
                if re.fullmatch(r'\d{9,15}', digits_only):
                    raw_phone = answer
                    break

        if raw_phone:
            normalized = re.sub(r'[\+\s\-\(\)]', '', raw_phone)
            logger.info(f"Calendly raw phone: '{raw_phone}' -> normalized: '{normalized}'")
            return normalized

        return None
