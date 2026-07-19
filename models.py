# Backward-compat shim — remove after Phase 8 (main.py imports updated).
from app.core.models import *  # noqa: F401, F403
from app.core.models import (  # noqa: F401
    Client, Lead, Message, PipelineStage, PromptTemplate,
    EmailSuppression, Document, UsageEvent, DailyStat,
    EmailCampaign, EmailCampaignStep, EmailCampaignEnrollment,
)
