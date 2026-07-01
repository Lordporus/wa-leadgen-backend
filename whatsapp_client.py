import requests
import logging
import time
import random
from datetime import datetime
from config import (
    WHATSAPP_ACCESS_TOKEN, WHATSAPP_PHONE_NUMBER_ID, 
    WHATSAPP_BUSINESS_ACCOUNT_ID, WHATSAPP_SIMULATE_HUMAN_DELAY
)

logger = logging.getLogger(__name__)

class WhatsAppClient:
    def __init__(self):
        self.access_token = WHATSAPP_ACCESS_TOKEN
        self.phone_number_id = WHATSAPP_PHONE_NUMBER_ID
        self.business_account_id = WHATSAPP_BUSINESS_ACCOUNT_ID
        self.base_url = f"https://graph.facebook.com/v17.0/{self.phone_number_id}/messages"
        
        # Anti-ban guardrails
        self.daily_cap = 50
        self.sends_today = 0
        self.current_date = datetime.now().date()

    def _check_rate_limit(self) -> bool:
        """Check daily cap and apply randomized delay. Return True if allowed to send."""
        today = datetime.now().date()
        if today > self.current_date:
            self.current_date = today
            self.sends_today = 0
            
        if self.sends_today >= self.daily_cap:
            logger.warning(f"WhatsApp daily send cap reached ({self.daily_cap}). Blocking outbound message.")
            return False
            
        if WHATSAPP_SIMULATE_HUMAN_DELAY:
            # Randomized delay between 3 and 10 seconds to mimic human sending
            delay = random.uniform(3.0, 10.0)
            logger.info(f"Applying random delay of {delay:.2f}s before sending WhatsApp message...")
            time.sleep(delay)
        else:
            logger.debug("Skipping artificial human delay for WhatsApp message sending.")
        
        self.sends_today += 1
        return True

    def send_message(self, to_phone: str, text: str) -> str | None:
        if not self._check_rate_limit():
            return None
            
        headers = {
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/json"
        }
        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": to_phone,
            "type": "text",
            "text": {
                "preview_url": False,
                "body": text
            }
        }
        
        try:
            response = requests.post(self.base_url, headers=headers, json=payload)
            response.raise_for_status()
            logger.info(f"Text message sent to {to_phone}.")
            data = response.json()
            return data.get("messages", [{}])[0].get("id")
        except requests.exceptions.RequestException as e:
            logger.error(f"Failed to send message to {to_phone}: {e}")
            if e.response is not None:
                logger.error(f"Error details: {e.response.text}")
            return None

    def send_template(self, to_phone: str, template_name: str, language_code: str = "en") -> str | None:
        """Send a pre-approved template message (required for initial outbound outreach)."""
        if not self._check_rate_limit():
            return None
            
        headers = {
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/json"
        }
        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": to_phone,
            "type": "template",
            "template": {
                "name": template_name,
                "language": {
                    "code": language_code
                }
            }
        }
        
        try:
            response = requests.post(self.base_url, headers=headers, json=payload)
            response.raise_for_status()
            logger.info(f"Template '{template_name}' sent to {to_phone}.")
            data = response.json()
            return data.get("messages", [{}])[0].get("id")
        except requests.exceptions.RequestException as e:
            logger.error(f"Failed to send template to {to_phone}: {e}")
            if e.response is not None:
                logger.error(f"Error details: {e.response.text}")
            return None

    def submit_template(self, name: str, category: str, components: list, language: str = "en") -> dict | None:
        """Submit a new message template to Meta for approval."""
        if not self.business_account_id:
            logger.error("WHATSAPP_BUSINESS_ACCOUNT_ID missing. Cannot submit template.")
            return None
            
        url = f"https://graph.facebook.com/v17.0/{self.business_account_id}/message_templates"
        headers = {
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/json"
        }
        payload = {
            "name": name,
            "category": category,
            "components": components,
            "language": language
        }
        
        try:
            response = requests.post(url, headers=headers, json=payload)
            response.raise_for_status()
            logger.info(f"Template '{name}' submitted for approval successfully.")
            return response.json()
        except requests.exceptions.RequestException as e:
            logger.error(f"Failed to submit template: {e}")
            if e.response is not None:
                logger.error(f"Error details: {e.response.text}")
            return None

    def get_template(self, name: str) -> dict | None:
        """Get template status from Meta."""
        if not self.business_account_id:
            logger.error("WHATSAPP_BUSINESS_ACCOUNT_ID missing. Cannot fetch template.")
            return None
            
        url = f"https://graph.facebook.com/v17.0/{self.business_account_id}/message_templates?name={name}"
        headers = {"Authorization": f"Bearer {self.access_token}"}
        
        try:
            response = requests.get(url, headers=headers)
            response.raise_for_status()
            data = response.json().get("data", [])
            return data[0] if data else None
        except requests.exceptions.RequestException as e:
            logger.error(f"Failed to fetch template {name}: {e}")
            return None
