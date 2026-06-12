from pyairtable import Table
import logging
from config import AIRTABLE_API_KEY, AIRTABLE_BASE_ID, AIRTABLE_TABLE_NAME

logger = logging.getLogger(__name__)

class AirtableClient:
    def __init__(self):
        if not all([AIRTABLE_API_KEY, AIRTABLE_BASE_ID, AIRTABLE_TABLE_NAME]):
            logger.warning("Airtable credentials not fully configured.")
            self.table = None
        else:
            self.table = Table(AIRTABLE_API_KEY, AIRTABLE_BASE_ID, AIRTABLE_TABLE_NAME)

    def add_lead(self, name: str, phone: str, source: str = "Apify - Google Maps"):
        """Add a new lead to Airtable."""
        if not self.table: return None
        try:
            record = self.table.create({
                "Name": name,
                "Phone": phone,
                "Source": source,
                "Status": "New Lead"
            })
            logger.info(f"Added lead to Airtable: {name}")
            return record
        except Exception as e:
            logger.error(f"Error adding lead to Airtable: {e}")
            return None

    def update_lead_status(self, phone: str, status: str):
        """Find lead by phone and update status."""
        if not self.table: return None
        try:
            # Note: The Airtable formula syntax requires matching the field exactly.
            formula = f"{{Phone}}='{phone}'"
            records = self.table.all(formula=formula)
            if records:
                record_id = records[0]['id']
                updated_record = self.table.update(record_id, {"Status": status})
                logger.info(f"Updated lead {phone} status to {status}")
                return updated_record
            else:
                logger.warning(f"Lead with phone {phone} not found for status update.")
                return None
        except Exception as e:
            logger.error(f"Error updating lead status in Airtable: {e}")
            return None
            
    def append_conversation(self, phone: str, message: str, sender: str):
        """Append message to a Conversation History field if it exists."""
        if not self.table: return None
        try:
            formula = f"{{Phone}}='{phone}'"
            records = self.table.all(formula=formula)
            if records:
                record = records[0]
                record_id = record['id']
                existing_history = record['fields'].get('Conversation History', '')
                new_entry = f"{sender}: {message}\n"
                new_history = existing_history + new_entry
                self.table.update(record_id, {"Conversation History": new_history})
        except Exception as e:
            logger.error(f"Error appending conversation to Airtable: {e}")
