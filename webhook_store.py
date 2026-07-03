import logging
from fastapi import BackgroundTasks

logger = logging.getLogger(__name__)

class WebhookStore:
    """
    A thin orchestration layer for the webhook endpoint.
    Routes primary operations synchronously and mirrors them asynchronously 
    to the secondary store using FastAPI BackgroundTasks, ensuring the webhook 
    never blocks on legacy CRM updates.
    """
    def __init__(self, primary, secondary, bg_tasks: BackgroundTasks):
        self.primary = primary
        self.secondary = secondary
        self.bg = bg_tasks

    def _safe_bg(self, fn, *args):
        try:
            fn(*args)
        except Exception as e:
            logger.error(f"[WebhookStore] Background mirror error: {e}")

    def get_lead(self, phone: str):
        return self.primary.get_lead(phone)

    def add_lead(self, name: str, phone: str, source: str = "Inbound WhatsApp"):
        result = self.primary.add_lead(name, phone, source)
        if self.secondary:
            self.bg.add_task(self._safe_bg, self.secondary.add_lead, name, phone, source)
        return result

    def append_message(self, phone: str, direction: str, message: str, msg_type: str = "text", wa_message_id: str | None = None) -> bool:
        result = self.primary.append_message(phone, direction, message, msg_type, wa_message_id)
        if self.secondary:
            self.bg.add_task(self._safe_bg, self.secondary.append_message, phone, direction, message, msg_type, wa_message_id)
        return result

    def update_lead_status(self, phone: str, status: str):
        result = self.primary.update_lead_status(phone, status)
        if self.secondary:
            self.bg.add_task(self._safe_bg, self.secondary.update_lead_status, phone, status)
        return result

    def update_message_status(self, wa_message_id: str, status: str):
        self.primary.update_message_status(wa_message_id, status)
        if self.secondary:
            self.bg.add_task(self._safe_bg, self.secondary.update_message_status, wa_message_id, status)

    def update_lead_info(self, phone: str, name: str | None, business_name: str | None):
        self.primary.update_lead_info(phone, name, business_name)
        if self.secondary:
            self.bg.add_task(self._safe_bg, self.secondary.update_lead_info, phone, name, business_name)

    def update_lead_score(self, phone: str, score: str):
        self.primary.update_lead_score(phone, score)
        if self.secondary:
            self.bg.add_task(self._safe_bg, self.secondary.update_lead_score, phone, score)
