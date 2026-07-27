from fastapi import APIRouter, Depends, HTTPException, Request, Response

from app.api.dependencies import get_client_key, limiter, require_api_key
from app.api.runtime import logger
from app.core.models import Client

router = APIRouter()

@router.post("/api/billing/checkout", dependencies=[Depends(require_api_key)])
@limiter.limit("10/minute", key_func=get_client_key)
def billing_checkout(request: Request, response: Response, client: Client = Depends(require_api_key)):
    """Create a Razorpay order for the client's plan upgrade."""
    from app.services.billing import create_subscription
    plan = request.query_params.get("plan", "base")
    try:
        result = create_subscription(client.id, plan)
        return result
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))


@router.post("/api/billing/webhook")
@limiter.limit("100/minute")
async def billing_webhook(request: Request, response: Response):
    """Receive and verify Razorpay webhook events."""
    from app.services.billing import verify_webhook_signature, handle_webhook

    signature = request.headers.get("X-Razorpay-Signature", "")
    body_bytes = await request.body()

    if not verify_webhook_signature(body_bytes, signature):
        logger.warning("Invalid Razorpay webhook signature rejected.")
        raise HTTPException(status_code=403, detail="Invalid signature")

    import json
    try:
        event_data = json.loads(body_bytes)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

    result = handle_webhook(event_data)
    return {"status": "ok", "result": result}


@router.get("/api/billing/status", dependencies=[Depends(require_api_key)])
@limiter.limit("60/minute", key_func=get_client_key)
def billing_status(request: Request, response: Response, client: Client = Depends(require_api_key)):
    """Return the client's current billing status and monthly usage summary."""
    from app.services.usage import get_monthly_usage, PLAN_LIMITS, DEFAULT_PLAN

    plan = client.plan_tier or "base"
    limits = PLAN_LIMITS.get(plan, PLAN_LIMITS[DEFAULT_PLAN])
    usage = get_monthly_usage(client.id)

    return {
        "plan_tier": plan,
        "subscription_status": client.subscription_status or "inactive",
        "usage": usage,
        "limits": limits,
    }
