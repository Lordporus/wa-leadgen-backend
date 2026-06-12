from pyairtable import Table
import logging
from datetime import datetime
from config import AIRTABLE_API_KEY, AIRTABLE_BASE_ID, AIRTABLE_TABLE_NAME

logger = logging.getLogger(__name__)

class AirtableClient:
    def __init__(self):
        if not all([AIRTABLE_API_KEY, AIRTABLE_BASE_ID, AIRTABLE_TABLE_NAME]):
            logger.warning("Airtable credentials not fully configured.")
            self.table = None
        else:
            self.table = Table(AIRTABLE_API_KEY, AIRTABLE_BASE_ID, AIRTABLE_TABLE_NAME)

    def get_lead(self, phone: str):
        """Check if a lead exists by phone."""
        if not self.table: return None
        try:
            formula = f"{{Phone number type}}='{phone}'"
            records = self.table.all(formula=formula)
            return records[0] if records else None
        except Exception as e:
            logger.error(f"Error getting lead: {e}")
            return None

    def add_lead(self, name: str, phone: str, source: str = "Apify - Google Maps"):
        """Add a new lead to Airtable."""
        if not self.table: return None
        try:
            record = self.table.create({
                "Name": name,
                "Phone number type": phone,
                "Source": source,
                "Status": "New Lead",
                "Created_At": datetime.now().isoformat()
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
            formula = f"{{Phone number type}}='{phone}'"
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
            
    def update_last_message(self, phone: str, message: str, sender: str):
        """Update Last_Message field to reflect the most recent message."""
        if not self.table: return None
        try:
            formula = f"{{Phone number type}}='{phone}'"
            records = self.table.all(formula=formula)
            if records:
                record_id = records[0]['id']
                self.table.update(record_id, {"Last_Message": f"{sender}: {message}"})
        except Exception as e:
            logger.error(f"Error updating Last_Message: {e}")

    def update_lead_info(self, phone: str, name: str, business_name: str):
        """Update lead Name and Business_Name fields."""
        if not self.table: return None
        try:
            formula = f"{{Phone number type}}='{phone}'"
            records = self.table.all(formula=formula)
            if records:
                record_id = records[0]['id']
                fields_to_update = {}
                if name: fields_to_update["Name"] = name
                if business_name: fields_to_update["Business_Name"] = business_name
                
                if fields_to_update:
                    self.table.update(record_id, fields_to_update)
                    logger.info(f"Updated info for {phone}: {fields_to_update}")
        except Exception as e:
            logger.error(f"Error updating lead info: {e}")
