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

    def _safe_bg(self, fn, *args, **kwargs):
        try:
            fn(*args, **kwargs)
        except Exception as e:
            logger.error(f"[WebhookStore] Background mirror error: {e}")

    def get_lead(self, phone: str, client_id: int):
        return self.primary.get_lead(phone, client_id=client_id)

    def add_lead(
        self,
        name: str,
        phone: str,
        source: str = "Inbound WhatsApp",
        *,
        client_id: int,
    ):
        if client_id is None:
            return None
        result = self.primary.add_lead(
            name,
            phone,
            source,
            client_id=client_id,
        )
        if self.secondary:
            self.bg.add_task(
                self._safe_bg,
                self.secondary.add_lead,
                name,
                phone,
                source,
                client_id=client_id,
            )
        return result

    def append_message(
        self,
        phone: str,
        direction: str,
        message: str,
        msg_type: str = "text",
        wa_message_id: str | None = None,
        *,
        client_id: int,
    ) -> bool:
        if client_id is None:
            return False
        result = self.primary.append_message(
            phone,
            direction,
            message,
            msg_type,
            wa_message_id,
            client_id=client_id,
        )
        if self.secondary:
            self.bg.add_task(
                self._safe_bg,
                self.secondary.append_message,
                phone,
                direction,
                message,
                msg_type,
                wa_message_id,
                client_id=client_id,
            )
        return result

    def update_lead_status(
        self,
        phone: str,
        status: str,
        client_id: int,
    ):
        if client_id is None:
            return None
        result = self.primary.update_lead_status(
            phone,
            status,
            client_id=client_id,
        )
        if self.secondary:
            self.bg.add_task(
                self._safe_bg,
                self.secondary.update_lead_status,
                phone,
                status,
                client_id=client_id,
            )
        return result

    def update_human_takeover_by_id(
        self,
        record_id: str | int,
        enabled: bool,
        client_id: int,
    ):
        result = self.primary.update_human_takeover_by_id(
            record_id,
            enabled,
            client_id=client_id,
        )
        phone = (result or {}).get("fields", {}).get("Phone number type")
        if self.secondary and phone:
            def mirror():
                secondary_record = self.secondary.get_lead(
                    phone,
                    client_id=client_id,
                )
                if secondary_record:
                    self.secondary.update_human_takeover_by_id(
                        secondary_record["id"],
                        enabled,
                        client_id=client_id,
                    )

            self.bg.add_task(self._safe_bg, mirror)
        return result

    def update_message_status(
        self,
        wa_message_id: str,
        status: str,
        client_id: int,
    ):
        if client_id is None:
            return
        self.primary.update_message_status(
            wa_message_id,
            status,
            client_id=client_id,
        )
        if self.secondary:
            self.bg.add_task(
                self._safe_bg,
                self.secondary.update_message_status,
                wa_message_id,
                status,
                client_id=client_id,
            )

    def update_lead_info(
        self,
        phone: str,
        name: str | None,
        business_name: str | None,
        client_id: int,
    ):
        if client_id is None:
            return
        self.primary.update_lead_info(
            phone,
            name,
            business_name,
            client_id=client_id,
        )
        if self.secondary:
            self.bg.add_task(
                self._safe_bg,
                self.secondary.update_lead_info,
                phone,
                name,
                business_name,
                client_id=client_id,
            )

    def update_lead_score(
        self,
        phone: str,
        score: str,
        client_id: int,
    ):
        if client_id is None:
            return
        self.primary.update_lead_score(
            phone,
            score,
            client_id=client_id,
        )
        if self.secondary:
            self.bg.add_task(
                self._safe_bg,
                self.secondary.update_lead_score,
                phone,
                score,
                client_id=client_id,
            )
