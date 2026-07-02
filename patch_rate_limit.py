import sys

with open('main.py', 'r') as f:
    content = f.read()

# 1. Add imports
import_block = """from contextlib import asynccontextmanager"""
new_import_block = """from contextlib import asynccontextmanager
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded"""
content = content.replace(import_block, new_import_block)

# 2. Add Limiter setup and keys
app_init = """app = FastAPI(title="WhatsApp Acquisition Backend", lifespan=lifespan)"""
new_app_init = """app = FastAPI(title="WhatsApp Acquisition Backend", lifespan=lifespan)

def get_client_key(request: Request) -> str:
    api_key = request.headers.get("X-API-Key")
    ip = get_remote_address(request)
    return f"client:{api_key}" if api_key else ip

def get_admin_key(request: Request) -> str:
    admin_secret = request.headers.get("X-Admin-Secret")
    ip = get_remote_address(request)
    return f"admin:{admin_secret}:{ip}" if admin_secret else ip

limiter = Limiter(key_func=get_remote_address)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)"""
content = content.replace(app_init, new_app_init)

# 3. Add decorators and request param

replacements = [
    (
        '@app.get("/api/settings")\ndef get_settings(client: Client = Depends(require_api_key)):',
        '@app.get("/api/settings")\n@limiter.limit("120/minute", key_func=get_client_key)\ndef get_settings(request: Request, client: Client = Depends(require_api_key)):'
    ),
    (
        '@app.patch("/api/settings")\ndef update_settings(body: SettingsUpdateBody, client: Client = Depends(require_api_key)):',
        '@app.patch("/api/settings")\n@limiter.limit("120/minute", key_func=get_client_key)\ndef update_settings(request: Request, body: SettingsUpdateBody, client: Client = Depends(require_api_key)):'
    ),
    (
        '@app.post("/api/admin/clients", dependencies=[Depends(require_admin_secret)])\ndef admin_create_client(body: AdminCreateClientBody):',
        '@app.post("/api/admin/clients", dependencies=[Depends(require_admin_secret)])\n@limiter.limit("10/minute", key_func=get_admin_key)\ndef admin_create_client(request: Request, body: AdminCreateClientBody):'
    ),
    (
        '@app.get("/api/stats/dashboard", dependencies=[Depends(require_api_key)])\ndef get_dashboard_stats():',
        '@app.get("/api/stats/dashboard", dependencies=[Depends(require_api_key)])\n@limiter.limit("120/minute", key_func=get_client_key)\ndef get_dashboard_stats(request: Request):'
    ),
    (
        '@app.get("/api/leads", dependencies=[Depends(require_api_key)])\ndef list_leads(stage: str | None = None):',
        '@app.get("/api/leads", dependencies=[Depends(require_api_key)])\n@limiter.limit("120/minute", key_func=get_client_key)\ndef list_leads(request: Request, stage: str | None = None):'
    ),
    (
        '@app.get("/api/leads/{lead_id}", dependencies=[Depends(require_api_key)])\ndef get_lead_detail(lead_id: str):',
        '@app.get("/api/leads/{lead_id}", dependencies=[Depends(require_api_key)])\n@limiter.limit("120/minute", key_func=get_client_key)\ndef get_lead_detail(request: Request, lead_id: str):'
    ),
    (
        '@app.get("/api/leads/{lead_id}/messages", dependencies=[Depends(require_api_key)])\ndef get_lead_messages(lead_id: str):',
        '@app.get("/api/leads/{lead_id}/messages", dependencies=[Depends(require_api_key)])\n@limiter.limit("120/minute", key_func=get_client_key)\ndef get_lead_messages(request: Request, lead_id: str):'
    ),
    (
        '@app.patch("/api/leads/{lead_id}/stage", dependencies=[Depends(require_api_key)])\ndef update_lead_stage(lead_id: str, body: StageUpdateBody):',
        '@app.patch("/api/leads/{lead_id}/stage", dependencies=[Depends(require_api_key)])\n@limiter.limit("120/minute", key_func=get_client_key)\ndef update_lead_stage(request: Request, lead_id: str, body: StageUpdateBody):'
    ),
    (
        '@app.get("/")\ndef read_root():',
        '@app.get("/")\n@limiter.limit("60/minute")\ndef read_root(request: Request):'
    ),
    (
        '@app.get("/webhook")\ndef verify_webhook(request: Request):',
        '@app.get("/webhook")\n@limiter.limit("10/minute")\ndef verify_webhook(request: Request):'
    ),
    (
        '@app.post("/webhook")\nasync def receive_message(request: Request, bg_tasks: BackgroundTasks):',
        '@app.post("/webhook")\n@limiter.limit("1000/minute")\nasync def receive_message(request: Request, bg_tasks: BackgroundTasks):'
    ),
    (
        '@app.get("/api/analytics/funnel", dependencies=[Depends(require_api_key)])\ndef analytics_funnel(client: Client = Depends(require_api_key)):',
        '@app.get("/api/analytics/funnel", dependencies=[Depends(require_api_key)])\n@limiter.limit("120/minute", key_func=get_client_key)\ndef analytics_funnel(request: Request, client: Client = Depends(require_api_key)):'
    ),
    (
        '@app.get("/api/analytics/response-time", dependencies=[Depends(require_api_key)])\ndef analytics_response_time(client: Client = Depends(require_api_key)):',
        '@app.get("/api/analytics/response-time", dependencies=[Depends(require_api_key)])\n@limiter.limit("120/minute", key_func=get_client_key)\ndef analytics_response_time(request: Request, client: Client = Depends(require_api_key)):'
    ),
    (
        '@app.get("/api/analytics/bookings", dependencies=[Depends(require_api_key)])\ndef analytics_bookings(client: Client = Depends(require_api_key)):',
        '@app.get("/api/analytics/bookings", dependencies=[Depends(require_api_key)])\n@limiter.limit("120/minute", key_func=get_client_key)\ndef analytics_bookings(request: Request, client: Client = Depends(require_api_key)):'
    )
]

for old, new in replacements:
    if old not in content:
        print(f"Failed to find:\\n{old}\\n")
    content = content.replace(old, new)

with open('main.py', 'w') as f:
    f.write(content)
print("Done modifying main.py")
