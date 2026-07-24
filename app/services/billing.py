"""
Razorpay billing integration — recurring subscriptions via Subscriptions API.

Plan tiers ("base", "agency") match usage.py PLAN_LIMITS keys exactly.
Pricing: ₹4,999/mo (Base), ₹14,999/mo (Agency).

Plans must be created once on Razorpay (via setup_plans() or Dashboard)
and their IDs stored in env vars RAZORPAY_PLAN_ID_BASE / _AGENCY.
"""

import hashlib
import hmac
import logging

import razorpay

from app.core.config import (
    RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET, RAZORPAY_WEBHOOK_SECRET,
    RAZORPAY_PLAN_ID_BASE, RAZORPAY_PLAN_ID_AGENCY,
)
from app.core.database import SessionLocal, is_configured
from app.core.models import Client

logger = logging.getLogger(__name__)

PLAN_CONFIG = {
    "base": {
        "amount": 499900,
        "currency": "INR",
        "description": "AI Sales OS — Base (₹4,999/mo)",
        "period": "monthly",
        "interval": 1,
    },
    "agency": {
        "amount": 1499900,
        "currency": "INR",
        "description": "AI Sales OS — Agency (₹14,999/mo)",
        "period": "monthly",
        "interval": 1,
    },
}

SUBSCRIPTION_TOTAL_COUNT = 1200


def _get_razorpay_client() -> razorpay.Client:
    if not RAZORPAY_KEY_ID or not RAZORPAY_KEY_SECRET:
        raise RuntimeError("RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET must be set.")
    return razorpay.Client(auth=(RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET))


def _get_plan_id(plan: str) -> str:
    plan_ids = {
        "base": RAZORPAY_PLAN_ID_BASE,
        "agency": RAZORPAY_PLAN_ID_AGENCY,
    }
    plan_id = plan_ids.get(plan, "")
    if not plan_id:
        raise RuntimeError(
            f"RAZORPAY_PLAN_ID_{plan.upper()} env var not set. "
            f"Run setup_plans() or create plans on the Razorpay Dashboard first."
        )
    return plan_id


def setup_plans() -> dict:
    """
    One-time setup: create Razorpay Plans for each tier.
    Run this once, then store the returned plan IDs as env vars.
    Returns: {"base": "plan_xxx", "agency": "plan_yyy"}
    """
    rz = _get_razorpay_client()
    result = {}
    for tier, cfg in PLAN_CONFIG.items():
        plan = rz.plan.create({
            "period": cfg["period"],
            "interval": cfg["interval"],
            "item": {
                "name": cfg["description"],
                "amount": cfg["amount"],
                "currency": cfg["currency"],
                "description": cfg["description"],
            },
            "notes": {"tier": tier},
        })
        result[tier] = plan["id"]
        logger.info(f"Created Razorpay plan for {tier}: {plan['id']}")
    return result


def create_subscription(client_id: int, plan: str) -> dict:
    """
    Create a Razorpay recurring subscription for the given client and plan.

    Uses the Subscriptions API with pre-created Plan IDs.
    Razorpay handles recurring charges automatically each billing cycle.
    Returns subscription details including short_url for customer auth payment.
    """
    if plan not in PLAN_CONFIG:
        raise ValueError(f"Unknown plan: {plan!r}. Must be one of: {list(PLAN_CONFIG.keys())}")

    if not is_configured():
        raise RuntimeError("Database not configured.")

    rz = _get_razorpay_client()
    plan_id = _get_plan_id(plan)

    with SessionLocal() as session:
        client = session.get(Client, client_id)
        if not client:
            raise ValueError(f"Client {client_id} not found.")

        if not client.razorpay_customer_id:
            customer = rz.customer.create({
                "name": client.name,
                "notes": {"client_id": str(client_id)},
            })
            client.razorpay_customer_id = customer["id"]
            session.commit()
            logger.info(f"Created Razorpay customer {customer['id']} for client {client_id}.")

        sub = rz.subscription.create({
            "plan_id": plan_id,
            "total_count": SUBSCRIPTION_TOTAL_COUNT,
            "customer_notify": 1,
            "notes": {
                "client_id": str(client_id),
                "plan_tier": plan,
            },
        })

        client.razorpay_subscription_id = sub["id"]
        client.plan_tier = plan
        client.subscription_status = "created"
        session.commit()

    logger.info(f"Created Razorpay subscription {sub['id']} for client {client_id}, plan={plan}.")
    return {
        "subscription_id": sub["id"],
        "plan": plan,
        "description": PLAN_CONFIG[plan]["description"],
        "short_url": sub.get("short_url"),
        "status": sub.get("status", "created"),
        "razorpay_key_id": RAZORPAY_KEY_ID,
    }


def verify_webhook_signature(payload_body: bytes, signature: str) -> bool:
    """Verify Razorpay webhook X-Razorpay-Signature using HMAC SHA256."""
    if not signature or not RAZORPAY_WEBHOOK_SECRET:
        return False
    expected = hmac.new(
        key=RAZORPAY_WEBHOOK_SECRET.encode("utf-8"),
        msg=payload_body,
        digestmod=hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


def handle_webhook(event_data: dict) -> str:
    """
    Process a verified Razorpay webhook event.

    Handles:
      - subscription.activated  → status = "active"
      - subscription.charged    → status = "active" (renewal, log billing cycle)
      - subscription.pending    → status = "past_due" (charge failed, retrying)
      - subscription.halted     → status = "halted" (all retries exhausted)
      - subscription.cancelled  → status = "cancelled"
      - subscription.completed  → status = "completed" (all cycles done)

    Returns a short status string for logging.
    """
    event_type = event_data.get("event", "")
    payload = event_data.get("payload", {})

    sub_entity = (payload.get("subscription", {}).get("entity", {})
                  if "subscription" in payload else {})

    rz_sub_id = sub_entity.get("id") or ""
    notes = sub_entity.get("notes") or {}
    client_id_str = notes.get("client_id", "")

    if not client_id_str:
        logger.warning(f"Razorpay webhook {event_type}: no client_id in notes, skipping.")
        return "no_client_id"

    try:
        client_id = int(client_id_str)
    except (ValueError, TypeError):
        logger.warning(f"Razorpay webhook {event_type}: invalid client_id={client_id_str!r}")
        return "invalid_client_id"

    if not is_configured():
        return "db_not_configured"

    status_map = {
        "subscription.activated": "active",
        "subscription.charged": "active",
        "subscription.pending": "past_due",
        "subscription.halted": "halted",
        "subscription.cancelled": "cancelled",
        "subscription.completed": "completed",
    }

    new_status = status_map.get(event_type)
    if not new_status:
        logger.info(f"Razorpay webhook: ignoring event {event_type}.")
        return "ignored"

    with SessionLocal() as session:
        client = session.get(Client, client_id)
        if not client:
            logger.warning(f"Razorpay webhook: client {client_id} not found.")
            return "client_not_found"

        client.subscription_status = new_status
        if rz_sub_id:
            client.razorpay_subscription_id = rz_sub_id

        plan_tier = notes.get("plan_tier")
        if plan_tier and plan_tier in PLAN_CONFIG:
            client.plan_tier = plan_tier

        session.commit()

    if event_type == "subscription.charged":
        paid_count = sub_entity.get("paid_count", 0)
        logger.info(
            f"Razorpay renewal: client {client_id}, cycle {paid_count}, "
            f"sub={rz_sub_id}"
        )
    else:
        logger.info(
            f"Razorpay webhook {event_type}: client {client_id} → "
            f"status={new_status}, sub={rz_sub_id or 'n/a'}"
        )

    return new_status
