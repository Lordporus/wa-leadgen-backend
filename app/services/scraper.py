"""
Phase 2 — Lead Source & Scraping Pipeline
==========================================
Source abstraction + cleaning + dedup + enriched storage for Dentists in Gurugram.

Run directly:
    python scraper.py

Sources currently implemented:
    - google_maps   (Apify Google Maps Scraper)

Adding a new source in future:
    1. Create a function `fetch_from_<source>(query, max_results) -> List[dict]`
       that returns a list of raw lead dicts with keys:
       name, phone, address, rating, review_count, source_label
    2. Register it in SOURCE_REGISTRY at the bottom of this file.
    3. Call `get_leads_from_source("your_source")` — pipeline handles the rest.
"""

import re
import logging
from datetime import datetime
from apify_client import ApifyClient
from app.core.config import APIFY_API_TOKEN
from app.store.store import get_store

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────
# CONSTANTS
# ─────────────────────────────────────────────
NICHE_QUERY        = "Dentist in Gurugram"
MAX_RESULTS        = 50          # Per Apify run
MIN_REVIEWS        = 0           # Filter: skip businesses with fewer reviews than this
APIFY_ACTOR        = "nwua9Gu5YrADL7ZDj"   # crawler-google-places (verified via API)


# ─────────────────────────────────────────────
# PHONE CLEANING & VALIDATION
# ─────────────────────────────────────────────
def clean_phone(raw: str) -> str | None:
    """
    Strip all non-digit characters and validate.
    Expected result: 10-digit Indian mobile starting with 6-9,
    or 12-digit with country code (91).
    Returns cleaned string or None if invalid.
    """
    if not raw:
        return None
    digits = re.sub(r"\D", "", raw)

    # Handle country code variants
    if digits.startswith("91") and len(digits) == 12:
        digits = digits[2:]   # strip leading 91
    if digits.startswith("0") and len(digits) == 11:
        digits = digits[1:]   # strip leading 0

    if len(digits) == 10 and digits[0] in "6789":
        return digits
    return None


# ─────────────────────────────────────────────
# DEDUPLICATION (against the active lead store)
# ─────────────────────────────────────────────
def is_duplicate(phone: str, store) -> bool:
    """Return True if phone already exists in the leads store."""
    return store.get_lead(phone) is not None


# ─────────────────────────────────────────────
# SOURCE: GOOGLE MAPS (Apify)
# ─────────────────────────────────────────────
def fetch_from_google_maps(query: str, max_results: int) -> list[dict]:
    """
    Fetch raw leads from Google Maps via Apify.
    Returns a list of dicts with keys:
        name, phone, address, rating, review_count, source_label
    """
    if not APIFY_API_TOKEN:
        logger.error("APIFY_API_TOKEN missing — cannot run Google Maps scraper.")
        return []

    client = ApifyClient(APIFY_API_TOKEN)
    run_input = {
        "searchStringsArray": [query],
        "maxCrawledPlacesPerSearch": max_results,
        "language": "en",
        "proxyConfig": {"useApifyProxy": True},
    }

    logger.info(f"Starting Apify Google Maps scrape: '{query}' (max {max_results})")
    run = client.actor(APIFY_ACTOR).call(run_input=run_input)
    dataset_id = run["defaultDatasetId"]
    raw_items = list(client.dataset(dataset_id).iterate_items())
    logger.info(f"Apify returned {len(raw_items)} raw results.")

    leads = []
    for item in raw_items:
        leads.append({
            "name":         item.get("title") or "",
            "phone":        item.get("phoneUnformatted") or item.get("phone") or "",
            "address":      item.get("address") or "",
            "rating":       item.get("totalScore"),      # float e.g. 4.3
            "review_count": item.get("reviewsCount"),    # int e.g. 87
            "source_label": "Google Maps - Gurugram",
        })
    return leads


# ─────────────────────────────────────────────
# CLEANING PIPELINE
# ─────────────────────────────────────────────
def clean_leads(raw_leads: list[dict]) -> list[dict]:
    """
    Apply cleaning rules:
    1. Must have a name
    2. Must have a valid Indian mobile number
    3. Must have at least MIN_REVIEWS reviews (filters ghost listings)
    Returns list of cleaned lead dicts.
    """
    cleaned = []
    for lead in raw_leads:
        phone = clean_phone(lead.get("phone", ""))
        if not phone:
            logger.debug(f"Skipped (invalid phone): {lead.get('name')}")
            continue
        if not lead.get("name", "").strip():
            logger.debug("Skipped (no name)")
            continue
        reviews = lead.get("review_count") or 0
        if reviews < MIN_REVIEWS:
            logger.debug(f"Skipped (too few reviews: {reviews}): {lead['name']}")
            continue

        cleaned.append({**lead, "phone": phone})

    logger.info(f"Cleaning: {len(raw_leads)} raw → {len(cleaned)} valid leads")
    return cleaned


# ─────────────────────────────────────────────
# STORAGE
# ─────────────────────────────────────────────
def store_leads(leads: list[dict], store) -> tuple[int, int]:
    """
    Push cleaned leads to the active store, skipping duplicates.
    Returns (added, skipped) counts.
    """
    added = skipped = 0
    for lead in leads:
        phone = lead["phone"]
        if is_duplicate(phone, store):
            logger.info(f"Duplicate skipped: {lead['name']} ({phone})")
            skipped += 1
            continue

        record = store.add_lead(
            name=lead["name"],
            phone=phone,
            source=lead["source_label"],
        )
        if not record:
            logger.error(f"Failed to store {lead['name']} ({phone})")
            continue

        # Enrich: stash address/rating as a seed system message on the lead.
        # (Mirrors the old Airtable Last_Message context line.)
        if lead.get("address") or lead.get("rating"):
            parts = []
            if lead.get("address"):   parts.append(f"📍 {lead['address']}")
            if lead.get("rating"):    parts.append(f"⭐ {lead['rating']} ({lead.get('review_count', 0)} reviews)")
            store.append_message(phone, direction="system",
                                 message="Scraped: " + " | ".join(parts), msg_type="system")

        logger.info(f"✅ Added: {lead['name']} ({phone})")
        added += 1

    return added, skipped


# ─────────────────────────────────────────────
# SOURCE REGISTRY (plug new sources in here)
# ─────────────────────────────────────────────
SOURCE_REGISTRY = {
    "google_maps": fetch_from_google_maps,
    # "instagram":   fetch_from_instagram,   # plug in Phase 2+
    # "website":     fetch_from_website,
}


# ─────────────────────────────────────────────
# PUBLIC INTERFACE
# ─────────────────────────────────────────────
def get_leads_from_source(source_name: str, query: str = NICHE_QUERY, max_results: int = MAX_RESULTS):
    """
    Main entry point — fetch, clean, dedup, and store leads from a named source.
    Usage:
        get_leads_from_source("google_maps")
        get_leads_from_source("google_maps", query="Dentist in Noida", max_results=30)
    """
    if source_name not in SOURCE_REGISTRY:
        logger.error(f"Unknown source: '{source_name}'. Available: {list(SOURCE_REGISTRY.keys())}")
        return

    airtable = get_store()
    if not getattr(airtable, "table", None):
        logger.error("Lead store not configured — cannot store leads.")
        return

    fetch_fn = SOURCE_REGISTRY[source_name]
    raw_leads = fetch_fn(query, max_results)
    cleaned   = clean_leads(raw_leads)
    added, skipped = store_leads(cleaned, airtable)

    logger.info(
        f"\n{'='*50}\n"
        f"  Scrape complete — source: {source_name}\n"
        f"  Raw: {len(raw_leads)} | Cleaned: {len(cleaned)} | Added: {added} | Duplicates skipped: {skipped}\n"
        f"{'='*50}"
    )
    return {"raw": len(raw_leads), "cleaned": len(cleaned), "added": added, "skipped": skipped}


# ─────────────────────────────────────────────
# CRON-READY ENTRY POINT
# ─────────────────────────────────────────────
if __name__ == "__main__":
    # Run with: python scraper.py
    # Suitable for cron: 0 9 * * 1 /path/to/venv/python /path/to/scraper.py
    get_leads_from_source("google_maps")
