# WhatsApp Acquisition Backend

> **BuildWithPorus** — Automated WhatsApp lead generation for dental clinics.
> Live on Render: `https://whatsapp-acquisition-backend.onrender.com`

---

## What This Is

A Python/FastAPI backend that powers an end-to-end WhatsApp customer acquisition
system for B2B agency clients. The current niche is **Dentists in Gurugram**.

The system implements a **5-Layer Architecture**:

| Layer | What It Does | Key File |
|---|---|---|
| **Lead Source** | Scrapes Google Maps for dentist phone numbers | `scraper.py` |
| **Conversation** | Receives/sends WhatsApp messages via Meta Cloud API | `whatsapp_client.py` / `main.py` |
| **Qualification** | AI (Gemini) qualifies leads in Hinglish | `gemini_client.py` |
| **Conversion** | Detects booking intent, updates lead status | `main.py` |
| **CRM** | Stores leads and pipeline state in Airtable | `airtable_client.py` |

---

## Tech Stack

| Component | Choice | Reason |
|---|---|---|
| Language | Python 3.11 | FastAPI ecosystem |
| Framework | FastAPI + Uvicorn | Async, fast, clean webhook handling |
| AI | Gemini 2.5 Flash (Google AI) | Fast, cheap, Hinglish-capable |
| CRM / DB | Airtable | MVP speed; replaces Postgres for Phase 1–6 |
| Scraping | Apify — Google Maps Scraper | Reliable, headless, no infra needed |
| Hosting | Render.com (free tier) | Auto-deploy from GitHub on push |
| Secrets | `.env` file (never committed) | `config.py` loads all keys at runtime |

---

## Project Structure

```
backend/
├── main.py              # FastAPI app — webhook routes, message handler
├── whatsapp_client.py   # Meta Cloud API — send messages
├── gemini_client.py     # Gemini AI — conversation logic + extraction
├── airtable_client.py   # Airtable CRUD — leads, status, messages
├── scraper.py           # Apify Google Maps scraper — pull & store leads
├── config.py            # Loads all env vars from .env
├── requirements.txt     # Python dependencies
├── render.yaml          # Render deployment config
├── .env                 # Secrets (gitignored)
├── .gitignore
└── docs/
    ├── niche.md         # Niche selection rationale
    └── schema.md        # Airtable DB schema v1
```

---

## How to Run Locally

```bash
# 1. Clone the repo
git clone https://github.com/Lordporus/wa-leadgen-backend.git
cd wa-leadgen-backend

# 2. Install dependencies
pip install -r requirements.txt

# 3. Add your secrets
cp .env.example .env   # then fill in the values

# 4. Start the server
uvicorn main:app --reload --port 8000
# Server runs at http://localhost:8000
# Webhook: http://localhost:8000/webhook (use ngrok to expose publicly for testing)

# 5. Run the lead scraper (standalone)
python scraper.py
```

---

## Environment Variables Required

| Key | Description |
|---|---|
| `WHATSAPP_ACCESS_TOKEN` | Meta permanent access token |
| `WHATSAPP_PHONE_NUMBER_ID` | Meta Phone Number ID |
| `WHATSAPP_VERIFY_TOKEN` | Webhook verify token (your secret string) |
| `GEMINI_API_KEY` | Google AI Studio API key |
| `AIRTABLE_API_KEY` | Airtable personal access token |
| `AIRTABLE_BASE_ID` | Airtable base ID (starts with `app`) |
| `AIRTABLE_TABLE_NAME` | Table name (currently `Leads`) |
| `APIFY_API_TOKEN` | Apify account API token |

---

## Deployment (Render)

1. Push to `main` branch on GitHub
2. Render auto-deploys via `render.yaml`
3. Set all env vars in Render Dashboard → Environment
4. After deploy, paste the live URL into Meta App Dashboard → Webhook

---

## Roadmap

See [`End-to-end-implementation_plan.md`](../End-to-end-implementation_plan.md) for the full 10-phase build plan.
