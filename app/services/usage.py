"""
Usage metering for billing — logs token consumption per tenant.

Supports event types: ai_response, document_ingested, embedding, email_sent.
Cost estimates use Gemini 2.5 Flash pricing as of July 2026.
"""

import logging
from datetime import datetime, timezone

from sqlalchemy import func, extract

from app.core.database import SessionLocal, is_configured
from app.core.models import UsageEvent, Client

logger = logging.getLogger(__name__)

COST_PER_1K_INPUT_TOKENS = 0.00015
COST_PER_1K_OUTPUT_TOKENS = 0.0006
COST_PER_1K_EMBEDDING_TOKENS = 0.00004

PLAN_LIMITS = {
    "base": {
        "max_ai_responses_per_month": 1000,
        "max_documents": 50,
        "max_tokens_per_month": 500_000,
        "max_emails_per_month": 500,
    },
    "agency": {
        "max_ai_responses_per_month": 5000,
        "max_documents": 200,
        "max_tokens_per_month": 2_000_000,
        "max_emails_per_month": 5000,
    },
}
DEFAULT_PLAN = "base"


def estimate_tokens(text: str) -> int:
    """Rough token estimate: ~4 characters per token."""
    if not text:
        return 0
    return max(1, len(text) // 4)


def log_usage(
    client_id: int,
    event_type: str,
    tokens_used: int,
    cost_estimate: float = 0.0,
) -> None:
    if not is_configured():
        return
    try:
        with SessionLocal() as session:
            event = UsageEvent(
                client_id=client_id,
                event_type=event_type,
                tokens_used=tokens_used,
                cost_estimate=cost_estimate,
            )
            session.add(event)
            session.commit()
    except Exception as e:
        logger.error(f"Failed to log usage event: {e}")


def get_monthly_usage(client_id: int) -> dict:
    """
    Aggregate current calendar month usage for a tenant.
    Returns: {total_tokens, total_cost, by_type: {event_type: {tokens, cost, count}}}
    """
    if not is_configured():
        return {"total_tokens": 0, "total_cost": 0.0, "by_type": {}}

    now = datetime.now(timezone.utc)
    try:
        with SessionLocal() as session:
            rows = (
                session.query(
                    UsageEvent.event_type,
                    func.sum(UsageEvent.tokens_used).label("tokens"),
                    func.sum(UsageEvent.cost_estimate).label("cost"),
                    func.count(UsageEvent.id).label("count"),
                )
                .filter(UsageEvent.client_id == client_id)
                .filter(extract("year", UsageEvent.created_at) == now.year)
                .filter(extract("month", UsageEvent.created_at) == now.month)
                .group_by(UsageEvent.event_type)
                .all()
            )

            by_type = {}
            total_tokens = 0
            total_cost = 0.0
            for row in rows:
                by_type[row.event_type] = {
                    "tokens": row.tokens or 0,
                    "cost": round(float(row.cost or 0), 6),
                    "count": row.count or 0,
                }
                total_tokens += row.tokens or 0
                total_cost += float(row.cost or 0)

            return {
                "total_tokens": total_tokens,
                "total_cost": round(total_cost, 6),
                "by_type": by_type,
            }
    except Exception as e:
        logger.error(f"Failed to get monthly usage: {e}")
        return {"total_tokens": 0, "total_cost": 0.0, "by_type": {}}


BLOCKED_SUBSCRIPTION_STATUSES = {"halted", "cancelled"}


def check_limit(client_id: int, limit_type: str, plan: str = DEFAULT_PLAN) -> tuple[bool, str | None]:
    """
    Check whether a tenant has exceeded a plan limit.

    limit_type: "ai_response" | "document_upload" | "tokens" | "email_sent"
    Returns: (allowed, reason) — (True, None) if under limit, (False, message) if over.
    Blocks all actions if subscription_status is halted or cancelled.
    """
    if is_configured():
        try:
            with SessionLocal() as session:
                client = session.get(Client, int(client_id))
                if client and client.subscription_status in BLOCKED_SUBSCRIPTION_STATUSES:
                    reason = (
                        f"Account suspended — subscription {client.subscription_status}. "
                        f"Please update your payment method to restore access."
                    )
                    logger.warning(f"Blocked: client {client_id} — {reason}")
                    return False, reason
        except Exception as e:
            logger.error(f"Failed to check subscription status: {e}")

    limits = PLAN_LIMITS.get(plan, PLAN_LIMITS[DEFAULT_PLAN])
    usage = get_monthly_usage(client_id)

    if limit_type == "ai_response":
        ai_usage = usage["by_type"].get("ai_response", {})
        count = ai_usage.get("count", 0)
        cap = limits["max_ai_responses_per_month"]
        if count >= cap:
            reason = f"Monthly AI response limit reached ({count}/{cap}). Upgrade your plan for more."
            log_usage(client_id, "limit_exceeded", 0, 0.0)
            logger.warning(f"Limit exceeded: client {client_id} — {reason}")
            return False, reason

    elif limit_type == "document_upload":
        doc_usage = usage["by_type"].get("document_ingested", {})
        count = doc_usage.get("count", 0)
        cap = limits["max_documents"]
        if count >= cap:
            reason = f"Monthly document upload limit reached ({count}/{cap}). Upgrade your plan for more."
            log_usage(client_id, "limit_exceeded", 0, 0.0)
            logger.warning(f"Limit exceeded: client {client_id} — {reason}")
            return False, reason

    elif limit_type == "tokens":
        total = usage["total_tokens"]
        cap = limits["max_tokens_per_month"]
        if total >= cap:
            reason = f"Monthly token limit reached ({total}/{cap}). Upgrade your plan for more."
            log_usage(client_id, "limit_exceeded", 0, 0.0)
            logger.warning(f"Limit exceeded: client {client_id} — {reason}")
            return False, reason

    elif limit_type == "email_sent":
        email_usage = usage["by_type"].get("email_sent", {})
        count = email_usage.get("count", 0)
        cap = limits.get("max_emails_per_month", 500)
        if count >= cap:
            reason = f"Monthly email send limit reached ({count}/{cap}). Upgrade your plan for more."
            log_usage(client_id, "limit_exceeded", 0, 0.0)
            logger.warning(f"Limit exceeded: client {client_id} — {reason}")
            return False, reason

    return True, None
