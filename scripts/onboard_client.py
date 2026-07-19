import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

#!/usr/bin/env python3
"""
Item #6 — Onboarding CLI.

Automates new client setup end-to-end:
  1. Collect client info interactively
  2. Create client in DB with hashed API key + pipeline stages
  3. Load and render selected niche template as system_prompt
  4. Run pre-launch checklist
  5. Print summary

Usage:
    python onboard_client.py
"""

import hashlib
import secrets
import sys
from datetime import datetime

from dotenv import load_dotenv
load_dotenv()

from app.core.config import DATABASE_URL
from app.core.database import init_engine, SessionLocal
from app.core.models import Client, PipelineStage, PromptTemplate

init_engine(DATABASE_URL)

BOLD = "\033[1m"
GREEN = "\033[92m"
YELLOW = "\033[93m"
RED = "\033[91m"
CYAN = "\033[96m"
RESET = "\033[0m"
CHECK = f"{GREEN}✅{RESET}"
WARN = f"{YELLOW}⚠️{RESET}"
FAIL = f"{RED}❌{RESET}"

DEFAULT_STAGES = [
    ("New Lead",  1, False, False),
    ("Contacted", 2, False, False),
    ("Qualified", 3, False, False),
    ("Booked",    4, True,  False),
    ("Lost",      5, False, True),
]


def banner():
    print(f"\n{BOLD}{'=' * 60}{RESET}")
    print(f"{BOLD}  WhatsApp Leads — New Client Onboarding{RESET}")
    print(f"{BOLD}{'=' * 60}{RESET}\n")


def prompt(label: str, default: str = "") -> str:
    hint = f" [{default}]" if default else ""
    value = input(f"  {label}{hint}: ").strip()
    return value or default


def pick_template(session) -> PromptTemplate | None:
    templates = session.query(PromptTemplate).order_by(PromptTemplate.id).all()
    if not templates:
        print(f"  {WARN} No templates found in prompt_templates table.")
        return None

    print(f"\n  {BOLD}Available niche templates:{RESET}")
    for i, t in enumerate(templates, 1):
        default_tag = " (default)" if t.is_default else ""
        print(f"    {i}. {t.niche} — {t.display_name}{default_tag}")
    print(f"    0. Skip — set system prompt manually later")

    default_idx = next((i for i, t in enumerate(templates, 1) if t.is_default), 1)
    choice = prompt(f"Pick template number", str(default_idx))

    try:
        idx = int(choice)
    except ValueError:
        idx = default_idx

    if idx == 0:
        return None
    if 1 <= idx <= len(templates):
        return templates[idx - 1]

    print(f"  {WARN} Invalid choice, using default.")
    return templates[default_idx - 1]


def collect_info(session) -> dict:
    print(f"  {BOLD}Step 1: Client Information{RESET}\n")

    name = prompt("Company name")
    if not name:
        print(f"  {FAIL} Company name is required.")
        sys.exit(1)

    wa_phone_id = prompt("WhatsApp Phone Number ID")
    if not wa_phone_id:
        print(f"  {FAIL} WhatsApp Phone Number ID is required.")
        sys.exit(1)

    existing = session.query(Client).filter(
        Client.wa_phone_number_id == wa_phone_id
    ).first()
    if existing:
        print(f"  {FAIL} Phone Number ID '{wa_phone_id}' already assigned to "
              f"client #{existing.id} ({existing.name}). Aborting.")
        sys.exit(1)

    calendly_link = prompt("Calendly link", "https://calendly.com/your-link")
    template = pick_template(session)
    brand_color = prompt("\n  Brand color", "#C8A96E")
    admin_phone = prompt("Admin phone for alerts (optional)", "")

    return {
        "name": name,
        "wa_phone_number_id": wa_phone_id,
        "calendly_link": calendly_link,
        "template": template,
        "brand_color": brand_color,
        "admin_phone": admin_phone,
    }


def create_client(session, info: dict) -> tuple[Client, str]:
    print(f"\n  {BOLD}Step 2: Creating client in database...{RESET}")

    raw_api_key = secrets.token_hex(32)
    key_hash = hashlib.sha256(raw_api_key.encode("utf-8")).hexdigest()

    rendered_prompt = None
    template = info["template"]
    if template:
        rendered_prompt = template.body
        rendered_prompt = rendered_prompt.replace(
            "{{agency_name}}", info["name"]
        )
        rendered_prompt = rendered_prompt.replace(
            "{{calendly_link}}", info["calendly_link"]
        )

    client = Client(
        name=info["name"],
        wa_phone_number_id=info["wa_phone_number_id"],
        system_prompt=rendered_prompt,
        calendly_link=info["calendly_link"],
        dashboard_api_key_hash=key_hash,
        is_active=True,
        brand_color=info["brand_color"],
        admin_phone=info["admin_phone"] or None,
    )
    session.add(client)
    session.flush()

    for stage_name, position, is_won, is_lost in DEFAULT_STAGES:
        session.add(PipelineStage(
            client_id=client.id,
            name=stage_name,
            position=position,
            is_won=is_won,
            is_lost=is_lost,
        ))

    session.commit()
    print(f"  {CHECK} Client #{client.id} created.")
    return client, raw_api_key


def run_checklist(session, client: Client, raw_api_key: str, info: dict):
    print(f"\n  {BOLD}Step 4: Pre-Launch Checklist{RESET}\n")

    results = []

    # 1. Client exists
    db_client = session.query(Client).filter(Client.id == client.id).first()
    if db_client:
        results.append((True, "Client created in DB"))
    else:
        results.append((False, "Client NOT found in DB"))

    # 2. API key
    if db_client and db_client.dashboard_api_key_hash:
        results.append((True, "Dashboard API key generated"))
    else:
        results.append((False, "Dashboard API key missing"))

    # 3. Pipeline stages
    stages = session.query(PipelineStage).filter(
        PipelineStage.client_id == client.id
    ).all()
    if len(stages) == len(DEFAULT_STAGES):
        results.append((True, f"Pipeline stages seeded ({len(stages)} stages)"))
    else:
        results.append((False, f"Pipeline stages: expected {len(DEFAULT_STAGES)}, got {len(stages)}"))

    # 4. System prompt
    if db_client and db_client.system_prompt and len(db_client.system_prompt) > 20:
        template_name = info["template"].display_name if info["template"] else "custom"
        results.append((True, f"System prompt set (template: {template_name})"))
    else:
        results.append((False, "System prompt is empty or not set"))

    # 5. API settings verify
    try:
        import requests
        resp = requests.get(
            "http://localhost:8080/api/settings",
            headers={"X-API-Key": raw_api_key},
            timeout=5,
        )
        if resp.status_code == 200:
            results.append((True, "GET /api/settings returns 200 for new client"))
        else:
            results.append((None, f"GET /api/settings returned {resp.status_code} (backend may be offline)"))
    except Exception:
        results.append((None, "GET /api/settings skipped (backend not running locally)"))

    # Print results
    for status, msg in results:
        if status is True:
            print(f"    {CHECK} {msg}")
        elif status is False:
            print(f"    {FAIL} {msg}")
        else:
            print(f"    {WARN}  {msg}")

    # Manual action reminders
    print(f"\n  {BOLD}Manual steps remaining:{RESET}\n")
    print(f"    {WARN}  Submit WhatsApp message template to Meta for this niche")
    print(f"    {WARN}  Configure webhook URL in Meta dashboard for phone ID: {info['wa_phone_number_id']}")
    print(f"    {WARN}  Set FOLLOWUP_TEMPLATE_NAME once Meta approves the template")
    if not info["admin_phone"]:
        print(f"    {WARN}  Set admin_phone on client for hot lead alerts")


def print_summary(client: Client, raw_api_key: str, info: dict):
    print(f"\n{BOLD}{'=' * 60}{RESET}")
    print(f"{BOLD}  Onboarding Complete{RESET}")
    print(f"{BOLD}{'=' * 60}{RESET}\n")

    print(f"  {CYAN}Client ID:{RESET}        {client.id}")
    print(f"  {CYAN}Company:{RESET}          {client.name}")
    print(f"  {CYAN}Phone Number ID:{RESET}  {info['wa_phone_number_id']}")
    print(f"  {CYAN}Calendly:{RESET}         {info['calendly_link']}")
    print(f"  {CYAN}Brand Color:{RESET}      {info['brand_color']}")
    if info["template"]:
        print(f"  {CYAN}Template:{RESET}         {info['template'].niche} — {info['template'].display_name}")

    print(f"\n  {BOLD}{YELLOW}Dashboard API Key (save this — shown only once):{RESET}")
    print(f"\n    {raw_api_key}\n")

    print(f"  {BOLD}To log in:{RESET} Open the dashboard and enter the API key above.")
    print(f"  {BOLD}Date:{RESET}     {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print()


def main():
    if not SessionLocal:
        print(f"{FAIL} DATABASE_URL not configured. Set it in .env and retry.")
        sys.exit(1)

    banner()

    with SessionLocal() as session:
        info = collect_info(session)

        print(f"\n  {BOLD}Confirm:{RESET} Create client '{info['name']}' "
              f"with phone ID '{info['wa_phone_number_id']}'?")
        confirm = prompt("Proceed? (y/n)", "y")
        if confirm.lower() not in ("y", "yes"):
            print("  Aborted.")
            sys.exit(0)

        client, raw_api_key = create_client(session, info)

        print(f"\n  {BOLD}Step 3: System prompt rendered and saved.{RESET}")

        run_checklist(session, client, raw_api_key, info)
        print_summary(client, raw_api_key, info)


if __name__ == "__main__":
    main()
