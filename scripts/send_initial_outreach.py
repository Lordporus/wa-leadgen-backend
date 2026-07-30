import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import argparse
import logging
from app.clients.airtable_client import AirtableClient
from app.clients.whatsapp_client import WhatsAppClient
from app.core.config import CLIENT_ID
from app.services import whatsapp_policy

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

def main(live: bool):
    airtable = AirtableClient()
    whatsapp = WhatsAppClient()
    
    template_name = "dentist_outreach_v1"
    records = airtable._search("{Status}='New Lead'", client_id=CLIENT_ID)
    logger.info(f"Found {len(records)} New Leads.")
    
    for r in records:
        phone = r.get("fields", {}).get("Phone number type")
        name = r.get("fields", {}).get("Name", "Doctor")
        
        if not phone:
            continue
            
        if live:
            logger.info(f"[LIVE] Sending {template_name} to {name} ({phone})")
            result = whatsapp_policy.send_immediate_template(
                client_id=CLIENT_ID,
                phone=phone,
                template_name=template_name,
                language="en",
                sender=whatsapp.send_template,
                action="local_initial_outreach_send",
            )
            if result.state == "sent":
                airtable.update_lead_status(
                    phone,
                    "Contacted",
                    client_id=CLIENT_ID,
                )
        else:
            logger.info(f"[DRY-RUN] Would send {template_name} to {name} ({phone}) and set Status = Contacted")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Send initial outreach to New Leads.")
    parser.add_argument(
        "--live",
        action="store_true",
        help="Use the production policy evaluator before any provider send.",
    )
    args = parser.parse_args()
    main(args.live)
