import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import argparse
import logging
from app.clients.airtable_client import AirtableClient
from app.clients.whatsapp_client import WhatsAppClient

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

def main(live: bool):
    airtable = AirtableClient()
    whatsapp = WhatsAppClient()
    
    # 1. Check approval status of dentist_outreach_v1
    template_name = "dentist_outreach_v1"
    template_info = whatsapp.get_template(template_name)
    if not template_info:
        logger.warning(f"Could not fetch template status for {template_name}. It might not exist.")
    else:
        status = template_info.get("status")
        logger.info(f"Template '{template_name}' status is: {status}")
        if live and status != "APPROVED":
            logger.error(f"Cannot send live outreach because template status is {status}")
            return
            
    # 2. Get New Leads
    records = airtable._search("{Status}='New Lead'")
    logger.info(f"Found {len(records)} New Leads.")
    
    for r in records:
        phone = r.get("fields", {}).get("Phone number type")
        name = r.get("fields", {}).get("Name", "Doctor")
        
        if not phone:
            continue
            
        if live:
            logger.info(f"[LIVE] Sending {template_name} to {name} ({phone})")
            res = whatsapp.send_template(phone, template_name)
            if res:
                airtable.update_lead_status(phone, "Contacted")
        else:
            logger.info(f"[DRY-RUN] Would send {template_name} to {name} ({phone}) and set Status = Contacted")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Send initial outreach to New Leads.")
    parser.add_argument("--live", action="store_true", help="Actually send messages and update Airtable.")
    args = parser.parse_args()
    main(args.live)
