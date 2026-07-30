import importlib.util
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.api.routers.whatsapp_policy import TemplateBody


def test_tenant_cannot_self_assert_meta_template_approval():
    tenant_claim: dict[str, object] = {
        "name": "follow_up",
        "language": "en",
        "category": "utility",
        "variables": [],
        "version": "1",
        "approval_status": "approved",
        "meta_status": "approved",
        "verification_reference": "tenant-claim",
    }
    with pytest.raises(ValidationError):
        TemplateBody.model_validate(tenant_claim)


def test_phase7_downgrade_refuses_to_delete_durable_opt_outs():
    path = (
        Path(__file__).resolve().parents[2]
        / "alembic"
        / "versions"
        / "0018_add_whatsapp_policy.py"
    )
    spec = importlib.util.spec_from_file_location("phase7_migration", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with pytest.raises(RuntimeError, match="forward-only"):
        module.downgrade()
