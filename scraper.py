import os
import logging
from apify_client import ApifyClient
from config import APIFY_API_TOKEN
from airtable_client import AirtableClient

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def run_scraper():
    if not APIFY_API_TOKEN:
        logger.error("APIFY_API_TOKEN is missing. Cannot run scraper.")
        return

    client = ApifyClient(APIFY_API_TOKEN)
    airtable = AirtableClient()

    # The apify/google-maps-scraper actor
    actor_id = "compass/google-maps-scraper"
    
    # Configuration for "Dentists in Gurugram"
    run_input = {
        "searchStringsArray": ["Dentist in Gurugram"],
        "maxCrawledPlacesPerSearch": 20, # Limit for initial run testing
        "language": "en",
        "proxyConfig": { "useApifyProxy": True }
    }

    logger.info("Starting Apify scraper for Dentists in Gurugram...")
    try:
        run = client.actor(actor_id).call(run_input=run_input)
        dataset_id = run["defaultDatasetId"]
        
        logger.info(f"Scraper finished. Fetching results from dataset {dataset_id}...")
        results = client.dataset(dataset_id).iterate_items()
        
        leads_added = 0
        for item in results:
            name = item.get("title")
            phone = item.get("phoneUnformatted")
            
            if name and phone:
                # Basic cleaning (remove formatting issues)
                clean_phone = phone.replace("+", "").replace(" ", "").replace("-", "")
                
                # Push to Airtable
                record = airtable.add_lead(name, clean_phone, source="Google Maps - Gurugram")
                if record:
                    leads_added += 1
                    
        logger.info(f"Successfully scraped and added {leads_added} leads to Airtable.")
    except Exception as e:
        logger.error(f"Failed to run scraper: {e}")

if __name__ == "__main__":
    run_scraper()
