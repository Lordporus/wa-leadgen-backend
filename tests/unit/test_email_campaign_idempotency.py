from contextlib import nullcontext
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

from app.email import email_campaigns
from app.email.email_client import EmailClient


def test_email_client_sends_stable_idempotency_header(monkeypatch):
    client = EmailClient()
    client.platform_enabled = True
    client.api_key = "offline-key"
    client.provider = "resend"
    client._sends_today = 0
    monkeypatch.setattr(client, "is_ready", lambda: True)

    response = SimpleNamespace(
        status_code=200,
        json=lambda: {"id": "offline-provider-id"},
        text="",
    )
    post = MagicMock(return_value=response)
    monkeypatch.setattr("app.email.email_client.requests.post", post)

    result = client.send_email(
        to="lead@example.com",
        subject="Offline test",
        text="No provider is contacted.",
        from_address="sender@example.com",
        idempotency_key="campaign-1-enrollment-2-step-0",
    )

    assert result.provider_message_id == "offline-provider-id"
    assert post.call_args.kwargs["headers"]["Idempotency-Key"] == (
        "campaign-1-enrollment-2-step-0"
    )


def test_campaign_tick_claims_due_rows_with_skip_locked(monkeypatch):
    query = MagicMock()
    query.filter.return_value = query
    query.order_by.return_value = query
    query.with_for_update.return_value = query
    query.first.return_value = None

    session = MagicMock()
    session.query.return_value = query
    session_context = nullcontext(session)

    monkeypatch.setattr(email_campaigns, "is_configured", lambda: True)
    monkeypatch.setattr(email_campaigns, "SessionLocal", lambda: session_context)

    result = email_campaigns.process_due_enrollments()

    assert result["processed"] == 0
    query.with_for_update.assert_called_once_with(skip_locked=True)


class _AttemptQuery:
    def __init__(self, session):
        self.session = session

    def filter(self, *args):
        return self

    def first(self):
        return self.session.attempt


class _AttemptSession:
    def __init__(self):
        self.attempt = None
        self.commits = 0

    def query(self, model):
        return _AttemptQuery(self)

    def add(self, attempt):
        self.attempt = attempt

    def flush(self):
        self.attempt.id = self.attempt.id or 1

    def commit(self):
        self.commits += 1


def _enrollment(run_id="run-offline"):
    return SimpleNamespace(
        id=12,
        campaign_id=4,
        client_id=1,
        delivery_run_id=run_id,
        next_run_at=None,
        updated_at=None,
    )


def test_delivery_attempt_survives_crash_and_reuses_provider_key():
    session = _AttemptSession()
    enrollment = _enrollment()
    now = datetime(2026, 7, 27, 12, 0)

    first, already_sent = email_campaigns._claim_delivery_attempt(
        session, enrollment, 0, now
    )
    first_key = first.idempotency_key

    # Simulate a crash after provider acceptance but before the final DB commit:
    # the durable attempt remains "sending". Recovery claims the same row/key.
    recovered, already_sent_on_retry = email_campaigns._claim_delivery_attempt(
        session, enrollment, 0, now
    )

    assert already_sent is False
    assert already_sent_on_retry is False
    assert session.commits == 2
    assert recovered is first
    assert recovered.idempotency_key == first_key
    assert recovered.attempt_count == 2

    recovered.state = "sent"
    completed, completed_already_sent = email_campaigns._claim_delivery_attempt(
        session, enrollment, 0, now
    )
    assert completed is recovered
    assert completed_already_sent is True
    assert session.commits == 2


def test_reenrollment_run_gets_a_new_delivery_identity():
    first_run = email_campaigns._new_delivery_run_id()
    second_run = email_campaigns._new_delivery_run_id()

    first = _enrollment(first_run)
    second = _enrollment(second_run)

    assert first_run != second_run
    assert email_campaigns._delivery_idempotency_key(first, 0) != (
        email_campaigns._delivery_idempotency_key(second, 0)
    )


def test_crash_recovery_send_reuses_persisted_attempt_key(monkeypatch):
    now = datetime(2026, 7, 27, 12, 0)
    enrollment = SimpleNamespace(
        id=12,
        campaign_id=4,
        client_id=1,
        lead_id=7,
        delivery_run_id="run-offline",
        current_step=0,
        status="active",
        next_run_at=now,
        updated_at=now,
        last_sent_at=None,
    )
    campaign = SimpleNamespace(id=4, client_id=1, status="active")
    lead = SimpleNamespace(
        id=7,
        email="lead@example.com",
        name="Lead",
        business_name="Business",
        status="New Lead",
        email_status="valid",
        updated_at=now,
    )
    client = SimpleNamespace(
        id=1,
        plan_tier="base",
        email_enabled=True,
        email_from_address="sender@example.com",
        email_from_name="Sender",
        email_reply_to=None,
        email_company_address="Offline address",
        email_footer_html=None,
        calendly_link=None,
        company_display_name="Offline company",
        name="Offline company",
    )
    step = SimpleNamespace(
        position=0,
        subject_template="Hello {{name}}",
        body_template="Body",
        delay_hours=0,
    )
    attempt = SimpleNamespace(
        state="sending",
        attempt_count=1,
        idempotency_key="persisted-crash-recovery-key",
        last_error=None,
        updated_at=now,
        provider_message_id=None,
        sent_at=None,
    )

    step_query = MagicMock()
    step_query.filter.return_value = step_query
    step_query.order_by.return_value = step_query
    step_query.all.return_value = [step]
    attempt_query = MagicMock()
    attempt_query.filter.return_value = attempt_query
    attempt_query.first.return_value = attempt

    session = MagicMock()
    session.get.side_effect = lambda model, object_id: {
        email_campaigns.EmailCampaign: campaign,
        email_campaigns.Lead: lead,
        email_campaigns.Client: client,
    }[model]
    session.query.side_effect = lambda model: (
        step_query
        if model is email_campaigns.EmailCampaignStep
        else attempt_query
    )

    monkeypatch.setattr(email_campaigns, "check_stop_conditions", lambda *a, **k: None)
    monkeypatch.setattr(email_campaigns, "check_limit", lambda *a, **k: (True, ""))
    monkeypatch.setattr(email_campaigns.email_client, "is_ready", lambda: True)
    send = MagicMock(
        return_value=SimpleNamespace(provider_message_id="provider-message-id")
    )
    monkeypatch.setattr(email_campaigns.email_client, "send_email", send)
    monkeypatch.setattr(
        email_campaigns,
        "build_unsubscribe_url",
        lambda *a, **k: "https://offline.invalid/unsubscribe",
    )
    monkeypatch.setattr(
        email_campaigns,
        "wrap_email_bodies",
        lambda **kwargs: ("Body", "<p>Body</p>"),
    )
    monkeypatch.setattr(email_campaigns, "log_usage", lambda *a, **k: None)

    outcome = email_campaigns._process_one(session, enrollment, now)

    assert outcome == "sent"
    assert send.call_args.kwargs["idempotency_key"] == (
        "persisted-crash-recovery-key"
    )
    assert attempt.state == "sent"
    assert enrollment.status == "completed"


def test_committed_sent_attempt_never_calls_provider(monkeypatch):
    session = _AttemptSession()
    enrollment = _enrollment()
    now = datetime(2026, 7, 27, 12, 0)
    attempt, _ = email_campaigns._claim_delivery_attempt(
        session, enrollment, 0, now
    )
    attempt.state = "sent"

    _, already_sent = email_campaigns._claim_delivery_attempt(
        session, enrollment, 0, now
    )

    assert already_sent is True
    assert session.commits == 1
