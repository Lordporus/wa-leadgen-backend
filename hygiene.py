import logging
from airtable_client import AirtableClient

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

def run_hygiene():
    airtable = AirtableClient()
    records = airtable._search("NOT({Phone number type}='')")
    logger.info(f"Scanning {len(records)} records for hygiene...")
    
    seen_phones = {}
    duplicates = []
    invalid_format = []
    
    for r in records:
        phone = r.get("fields", {}).get("Phone number type", "")
        name = r.get("fields", {}).get("Name", "Unknown")
        
        # Check invalid format (assuming Indian numbers for this niche, ~10-12 digits)
        if not phone.isdigit() or len(phone) < 10:
            invalid_format.append((name, phone))
            
        # Check duplicates
        if phone in seen_phones:
            duplicates.append((name, phone))
        else:
            seen_phones[phone] = True
            
    if duplicates:
        logger.info(f"Found {len(duplicates)} duplicate phone numbers:")
        for name, phone in duplicates:
            logger.info(f"  - {name} ({phone})")
    else:
        logger.info("No duplicates found.")
        
    if invalid_format:
        logger.info(f"Found {len(invalid_format)} invalid phone numbers:")
        for name, phone in invalid_format:
            logger.info(f"  - {name} ({phone})")
    else:
        logger.info("No invalid phone formats found.")

if __name__ == "__main__":
    run_hygiene()
