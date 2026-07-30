"""
Phase 7 — SQLAlchemy ORM models.

Mirrors the Phase 1–6 Airtable `Leads` table, with two improvements:
  1. `Last_Message` text-blob is normalised into a proper `messages` table.
  2. Forward-compat `client_id` FK (defaults to tenant #1) for Phase 8 multi-tenancy.

The :attr:`Lead.last_message` property reconstructs the *exact* text-blob
format that Airtable used, so `gemini_client.parse_conversation_history()`
works against Postgres data with zero changes:

    [YYYY-MM-DD HH:MM:SS] INBOUND (text): message body
    [YYYY-MM-DD HH:MM:SS] OUTBOUND (text): reply body
    [YYYY-MM-DD HH:MM:SS] SYSTEM (system): <event note>
"""

from datetime import datetime, date as date_type, timezone
from uuid import uuid4
from sqlalchemy import (
    Integer, String, Text, DateTime, Date, ForeignKey, Index, Boolean, Float,
    CheckConstraint, UniqueConstraint, text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import relationship, Mapped, mapped_column
from pgvector.sqlalchemy import Vector
from app.core.database import Base


def utc_now() -> datetime:
    """Return an aware UTC timestamp for timezone-aware Phase 7 columns."""
    return datetime.now(timezone.utc)


class Client(Base):
    """
    Agency tenant.
    Phase 7: single default client (id=1).
    Phase 8: one row per client — holds per-client Gemini prompt, WhatsApp
             phone number ID, Calendly link and follow-up template.
    """
    __tablename__ = "clients"
    __table_args__ = (
        CheckConstraint(
            "wa_phone_number_id IS NULL OR wa_phone_number_id = trim(wa_phone_number_id)",
            name="ck_clients_wa_phone_number_id_trimmed",
        ),
        Index(
            "uidx_clients_active_wa_phone",
            "wa_phone_number_id",
            unique=True,
            postgresql_where=text("wa_phone_number_id IS NOT NULL AND is_active"),
            sqlite_where=text("wa_phone_number_id IS NOT NULL AND is_active"),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)

    # ── Phase 8: per-client config ────────────────────────────────────────
    wa_phone_number_id: Mapped[str | None] = mapped_column(String(50),  nullable=True)
    # Phase 7 provider ownership. Tokens remain in the process secret store;
    # the tenant row holds only the environment-variable name.
    wa_business_account_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    wa_access_token_env_var: Mapped[str | None] = mapped_column(String(100), nullable=True)
    system_prompt:      Mapped[str | None] = mapped_column(Text,        nullable=True)
    followup_template:  Mapped[str | None] = mapped_column(String(100), nullable=True)
    calendly_link:      Mapped[str | None] = mapped_column(String(255), nullable=True)

    # ── F6: onboarding / multi-tenant auth ────────────────────────────────
    dashboard_api_key_hash: Mapped[str | None] = mapped_column(String(64), unique=True, nullable=True)
    is_active:              Mapped[bool]        = mapped_column(default=True)
    admin_note:             Mapped[str | None]  = mapped_column(Text, nullable=True)

    # ── White-label branding ────────────────────────────────────────────────
    brand_color:          Mapped[str | None] = mapped_column(String(20),  default="#C8A96E", nullable=True)
    logo_url:             Mapped[str | None] = mapped_column(String(500), nullable=True)
    company_display_name: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # ── AI scoring config ──────────────────────────────────────────────────────
    # Stored as an integer 0-100. Default 70 matches the hardcoded
    # CONFIDENCE_THRESHOLD in guardrails.py. NOT wired into scoring logic
    # yet — this field only persists the setting. Integration is a separate task.
    hot_lead_threshold: Mapped[int] = mapped_column(Integer, default=70, server_default="70", nullable=False)

    # ── F6b: multi-tenant scheduler jobs ──────────────────────────────────
    admin_phone: Mapped[str | None] = mapped_column(String(50), nullable=True)
    # TODO: Before actively using this field for per-client API calls,
    # it must be encrypted at rest (e.g. via Supabase Vault or
    # application-level encryption). Do not store or use real per-client
    # tokens in plaintext in production.
    calendly_api_token: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # ── Billing / Razorpay ───────────────────────────────────────────────
    razorpay_customer_id:     Mapped[str | None] = mapped_column(String(100), nullable=True)
    razorpay_subscription_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    subscription_status:      Mapped[str | None] = mapped_column(String(30), default="inactive", nullable=True)
    plan_tier:                Mapped[str | None] = mapped_column(String(20), default="base", nullable=True)

    # ── Sprint 8: agency sub-accounts ─────────────────────────────────────
    # role:      "owner"       → standalone tenant (default; all existing rows)
    #            "agency"      → parent tenant that provisions sub-accounts
    #            "sub_account" → child tenant owned by an agency
    # agency_id: self-FK to the parent agency's client id (NULL unless sub_account)
    role:       Mapped[str]        = mapped_column(String(20), default="owner", server_default="owner", nullable=False)
    agency_id:  Mapped[int | None] = mapped_column(ForeignKey("clients.id"), nullable=True)

    # ── Email outreach (Phase E1 schema; send path is E2) ─────────────────
    # Platform keys stay in env (RESEND_API_KEY). Per-tenant BYOK is deferred;
    # email_api_key_encrypted is reserved and must not hold plaintext keys.
    email_enabled:        Mapped[bool]       = mapped_column(
        Boolean, default=False, server_default="false", nullable=False
    )
    email_provider:       Mapped[str]        = mapped_column(
        String(30), default="resend", server_default="resend", nullable=False
    )
    email_from_address:   Mapped[str | None] = mapped_column(String(255), nullable=True)
    email_from_name:      Mapped[str | None] = mapped_column(String(255), nullable=True)
    email_reply_to:       Mapped[str | None] = mapped_column(String(255), nullable=True)
    email_company_address: Mapped[str | None] = mapped_column(String(500), nullable=True)
    email_footer_html:    Mapped[str | None] = mapped_column(Text, nullable=True)
    email_api_key_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)

    leads: Mapped[list["Lead"]] = relationship(back_populates="client")
    pipeline_stages: Mapped[list["PipelineStage"]] = relationship(
        back_populates="client", order_by="PipelineStage.position"
    )
    email_suppressions: Mapped[list["EmailSuppression"]] = relationship(
        back_populates="client", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"<Client id={self.id} name={self.name!r}>"


class Lead(Base):
    """
    Single source of truth for a prospect. `phone` is unique within a tenant
    and stored without `+`/spaces — matching the Airtable convention
    (see docs/schema.md: "Phone number type").

    Email (Phase E1): optional secondary contact. Phone remains required.
    Uniqueness is tenant-scoped: one email per client when email is set.
    """
    __tablename__ = "leads"
    __table_args__ = (
        UniqueConstraint("client_id", "phone", name="uq_leads_client_phone"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    phone: Mapped[str] = mapped_column(String(20), nullable=False)
    name: Mapped[str] = mapped_column(String(255), default="WhatsApp User")
    source: Mapped[str | None] = mapped_column(String(100), nullable=True)
    status: Mapped[str] = mapped_column(String(50), default="New Lead", index=True)
    business_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    lead_score: Mapped[str | None] = mapped_column(String(20), nullable=True)
    lead_score_numeric: Mapped[int | None] = mapped_column(Integer, nullable=True)
    notified_hot_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    is_human_takeover: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    whatsapp_opted_out_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # ── Email contact (Phase E1) ──────────────────────────────────────────
    # email_status: unknown | valid | bounced | complained | unsubscribed
    email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    email_status: Mapped[str | None] = mapped_column(String(30), nullable=True)
    email_opt_in_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    email_opt_in_source: Mapped[str | None] = mapped_column(String(100), nullable=True)
    client_id: Mapped[int] = mapped_column(
        ForeignKey("clients.id"), default=1, nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=datetime.utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow
    )

    messages: Mapped[list["Message"]] = relationship(
        back_populates="lead",
        cascade="all, delete-orphan",
        order_by="Message.created_at",
    )
    client: Mapped["Client"] = relationship(back_populates="leads")

    @property
    def last_message(self) -> str:
        """
        Reconstruct the Airtable `Last_Message` text-blob from `messages`.

        Format per line (must match airtable_client.append_message):
            [YYYY-MM-DD HH:MM:SS] DIRECTION (msg_type): body
        where DIRECTION is upper-cased (INBOUND / OUTBOUND / SYSTEM).
        """
        lines = []
        for m in self.messages:
            ts = m.created_at.strftime("%Y-%m-%d %H:%M:%S") if m.created_at else ""
            direction = (m.direction or "").upper()
            lines.append(f"[{ts}] {direction} ({m.msg_type}): {m.body}")
        return "\n".join(lines)

    def __repr__(self) -> str:
        return f"<Lead id={self.id} phone={self.phone!r} status={self.status!r}>"


# Sprint 10: composite for the hottest tenant-scoped query — "leads of a
# client filtered by status" (dashboard funnel, stage boards, list filters).
# Postgres does not auto-index the client_id FK, so this also covers the
# common client_id-only lookups as a leading-column prefix.
Index("idx_leads_client_status", Lead.client_id, Lead.status)
# Phase E1: tenant-scoped email uniqueness (NULL emails allowed many times).
Index(
    "uq_leads_client_email",
    Lead.client_id,
    Lead.email,
    unique=True,
    postgresql_where=text("email IS NOT NULL"),
)
Index("idx_leads_client_email", Lead.client_id, Lead.email)


class Message(Base):
    """Append-only conversation log. Replaces the Airtable long-text field.

    Phase E1: multi-channel fields. Existing WhatsApp rows default to
    channel='whatsapp'; wa_message_id kept for back-compat (provider_message_id
    is the generic id for email / future channels).
    """
    __tablename__ = "messages"
    __table_args__ = (UniqueConstraint("outbound_intent_id", name="uq_messages_outbound_intent"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    lead_id: Mapped[int] = mapped_column(
        ForeignKey("leads.id", ondelete="CASCADE"), nullable=False
    )
    direction: Mapped[str] = mapped_column(String(10), nullable=False)  # INBOUND/OUTBOUND/SYSTEM
    msg_type: Mapped[str] = mapped_column(String(20), default="text")
    body: Mapped[str | None] = mapped_column(Text, nullable=True)
    wa_message_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    status: Mapped[str | None] = mapped_column(String(50), nullable=True)
    # ── Multi-channel (Phase E1) ──────────────────────────────────────────
    # channel: whatsapp | email (future: sms, etc.)
    channel: Mapped[str] = mapped_column(
        String(20), default="whatsapp", server_default="whatsapp", nullable=False
    )
    subject: Mapped[str | None] = mapped_column(String(500), nullable=True)
    provider_message_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    thread_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    email_headers: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    # Not named "metadata" — reserved on SQLAlchemy Declarative API.
    provider_metadata: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    outbound_intent_id: Mapped[int | None] = mapped_column(
        ForeignKey("whatsapp_outbound_intents.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=datetime.utcnow
    )

    lead: Mapped["Lead"] = relationship(back_populates="messages")

    def __repr__(self) -> str:
        return f"<Message id={self.id} lead_id={self.lead_id} dir={self.direction!r}>"


# Convenient composite index for the follow-up job's "lead by status" queries.
Index("idx_messages_lead_id", Message.lead_id)
# Sprint 10: composite for "a lead's messages by direction" — response-time
# rollups and INBOUND/OUTBOUND counts in analytics.py. Its leading lead_id
# column also serves plain lead_id lookups.
Index("idx_messages_lead_direction", Message.lead_id, Message.direction)
# Phase E1: webhook lookup by provider id + channel filters on a lead.
Index("idx_messages_provider_message_id", Message.provider_message_id)
Index("idx_messages_lead_channel", Message.lead_id, Message.channel)


class DualWriteFailure(Base):
    """Durable, tenant-scoped record of a contained Postgres shadow failure."""

    __tablename__ = "dual_write_failures"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_dual_write_failures_idempotency_key"),
        Index("idx_dual_write_failures_open", "client_id", "resolved_at", "last_failed_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    client_id: Mapped[int] = mapped_column(
        ForeignKey("clients.id", ondelete="CASCADE"), nullable=False
    )
    operation: Mapped[str] = mapped_column(String(80), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(64), nullable=False)
    reference: Mapped[str | None] = mapped_column(String(255), nullable=True)
    error_type: Mapped[str] = mapped_column(String(120), nullable=False)
    error_message: Mapped[str] = mapped_column(Text, nullable=False)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    first_failed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
    last_failed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolution_note: Mapped[str | None] = mapped_column(Text, nullable=True)

    client: Mapped["Client"] = relationship()


class WhatsAppWebhookEvent(Base):
    """Durable WhatsApp ingress receipt and dead-letter/replay record."""

    __tablename__ = "whatsapp_webhook_events"
    __table_args__ = (
        UniqueConstraint("client_id", "event_kind", "event_id", name="uq_whatsapp_webhook_event"),
        Index("idx_whatsapp_webhook_events_state", "state", "received_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    client_id: Mapped[int] = mapped_column(
        ForeignKey("clients.id", ondelete="CASCADE"), nullable=False
    )
    event_kind: Mapped[str] = mapped_column(String(20), nullable=False)
    event_id: Mapped[str] = mapped_column(String(255), nullable=False)
    correlation_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    phone_number_id: Mapped[str] = mapped_column(String(255), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    state: Mapped[str] = mapped_column(String(30), nullable=False, default="received")
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    rq_job_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    dead_lettered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    client: Mapped["Client"] = relationship()


class WhatsAppOutboundIntent(Base):
    """One durable, tenant-scoped WhatsApp reply intent per inbound event/version."""

    __tablename__ = "whatsapp_outbound_intents"
    __table_args__ = (
        UniqueConstraint(
            "client_id", "inbound_event_id", "reply_version",
            name="uq_whatsapp_outbound_intent_reply",
        ),
        UniqueConstraint(
            "client_id", "provider_message_id",
            name="uq_whatsapp_outbound_intent_provider_message",
        ),
        Index("idx_whatsapp_outbound_intents_state", "state", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id", ondelete="CASCADE"), nullable=False)
    inbound_event_id: Mapped[int] = mapped_column(
        ForeignKey("whatsapp_webhook_events.id", ondelete="CASCADE"), nullable=False
    )
    reply_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    recipient_phone: Mapped[str] = mapped_column(String(50), nullable=False)
    body: Mapped[str | None] = mapped_column(Text, nullable=True)
    state: Mapped[str] = mapped_column(String(30), nullable=False, default="pending")
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    provider_message_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    provider_status: Mapped[str | None] = mapped_column(String(30), nullable=True)
    failure_category: Mapped[str | None] = mapped_column(String(50), nullable=True)
    failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    correlation_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    client: Mapped["Client"] = relationship()


class WhatsAppConsentRecord(Base):
    """Current tenant-scoped proof of WhatsApp consent for one phone."""

    __tablename__ = "whatsapp_consent_records"
    __table_args__ = (
        UniqueConstraint("client_id", "phone", name="uq_whatsapp_consent_client_phone"),
        Index("idx_whatsapp_consent_lookup", "client_id", "phone", "revoked_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    client_id: Mapped[int] = mapped_column(
        ForeignKey("clients.id", ondelete="CASCADE"), nullable=False
    )
    phone: Mapped[str] = mapped_column(String(50), nullable=False)
    source: Mapped[str] = mapped_column(String(100), nullable=False)
    consented_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    evidence_reference: Mapped[str | None] = mapped_column(String(500), nullable=True)
    policy_version: Mapped[str] = mapped_column(String(50), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revocation_reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )


class WhatsAppOptOut(Base):
    """Durable do-not-contact state. Consent writes never delete this row."""

    __tablename__ = "whatsapp_opt_outs"
    __table_args__ = (
        UniqueConstraint("client_id", "phone", name="uq_whatsapp_opt_out_client_phone"),
        Index("idx_whatsapp_opt_out_lookup", "client_id", "phone"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    client_id: Mapped[int] = mapped_column(
        ForeignKey("clients.id", ondelete="CASCADE"), nullable=False
    )
    phone: Mapped[str] = mapped_column(String(50), nullable=False)
    opted_out_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    reason: Mapped[str] = mapped_column(String(255), nullable=False)
    source: Mapped[str] = mapped_column(String(100), nullable=False)
    inbound_event_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    policy_version: Mapped[str] = mapped_column(String(50), nullable=False)


class WhatsAppTenantPolicy(Base):
    """Tenant-owned WhatsApp contact limits and outbound kill switch."""

    __tablename__ = "whatsapp_tenant_policies"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    client_id: Mapped[int] = mapped_column(
        ForeignKey("clients.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    outbound_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    timezone: Mapped[str] = mapped_column(
        String(64), nullable=False, default="UTC", server_default="UTC"
    )
    quiet_hours_start: Mapped[str | None] = mapped_column(String(5), nullable=True)
    quiet_hours_end: Mapped[str | None] = mapped_column(String(5), nullable=True)
    frequency_window_seconds: Mapped[int] = mapped_column(
        Integer, nullable=False, default=3600, server_default="3600"
    )
    max_messages_per_window: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    daily_cap: Mapped[int] = mapped_column(
        Integer, nullable=False, default=50, server_default="50"
    )
    excluded_lead_stages: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=lambda: ["Booked", "Lost"]
    )
    policy_version: Mapped[str] = mapped_column(
        String(50), nullable=False, default="phase7-v1", server_default="phase7-v1"
    )
    hot_lead_template_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    hot_lead_template_language: Mapped[str | None] = mapped_column(String(20), nullable=True)
    booking_alert_template_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    booking_alert_template_language: Mapped[str | None] = mapped_column(String(20), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )


class WhatsAppTemplate(Base):
    """Tenant registry entry for a Meta-approved WhatsApp template version."""

    __tablename__ = "whatsapp_templates"
    __table_args__ = (
        UniqueConstraint(
            "client_id", "name", "language", "version",
            name="uq_whatsapp_template_version",
        ),
        Index(
            "idx_whatsapp_template_approved",
            "client_id", "name", "language", "approval_status", "retired_at",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    client_id: Mapped[int] = mapped_column(
        ForeignKey("clients.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    language: Mapped[str] = mapped_column(String(20), nullable=False)
    category: Mapped[str] = mapped_column(String(30), nullable=False)
    variables: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    version: Mapped[str] = mapped_column(String(50), nullable=False)
    approval_status: Mapped[str] = mapped_column(String(30), nullable=False)
    meta_status: Mapped[str] = mapped_column(String(30), nullable=False)
    verification_reference: Mapped[str | None] = mapped_column(String(500), nullable=True)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    verification_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    meta_template_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    verified_waba_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    verified_phone_number_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    meta_variable_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    component_signature: Mapped[list[dict]] = mapped_column(
        JSONB, nullable=False, default=list
    )
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class WhatsAppPolicyDecision(Base):
    """Content-minimised audit record for every WhatsApp policy outcome."""

    __tablename__ = "whatsapp_policy_decisions"
    __table_args__ = (
        Index("idx_whatsapp_policy_audit_tenant_time", "client_id", "created_at"),
        Index("idx_whatsapp_policy_audit_phone_time", "client_id", "phone", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    audit_key: Mapped[str] = mapped_column(
        String(36), unique=True, nullable=False, default=lambda: str(uuid4())
    )
    client_id: Mapped[int] = mapped_column(
        ForeignKey("clients.id", ondelete="CASCADE"), nullable=False
    )
    phone: Mapped[str] = mapped_column(String(50), nullable=False)
    action: Mapped[str] = mapped_column(String(50), nullable=False)
    decision: Mapped[str] = mapped_column(String(20), nullable=False)
    reason_code: Mapped[str] = mapped_column(String(80), nullable=False)
    policy_version: Mapped[str] = mapped_column(String(50), nullable=False)
    session_open: Mapped[bool] = mapped_column(Boolean, nullable=False)
    template_id: Mapped[int | None] = mapped_column(
        ForeignKey("whatsapp_templates.id", ondelete="SET NULL"), nullable=True
    )
    outbound_intent_id: Mapped[int | None] = mapped_column(
        ForeignKey("whatsapp_outbound_intents.id", ondelete="SET NULL"), nullable=True
    )
    override_reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    correlation_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    provider_outcome: Mapped[str | None] = mapped_column(String(40), nullable=True)
    provider_failure_category: Mapped[str | None] = mapped_column(
        String(80), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class PipelineStage(Base):
    """
    Phase 8 — per-client ordered pipeline stage.

    Replaces the hardcoded status strings in main.py with a DB-driven list
    that each client can customise without a code deploy.

    is_won  → stage counts as a closed-won deal (e.g. "Booked").
    is_lost → stage counts as a closed-lost deal (e.g. "Lost").
    """
    __tablename__ = "pipeline_stages"

    id:        Mapped[int]  = mapped_column(Integer, primary_key=True)
    client_id: Mapped[int]  = mapped_column(ForeignKey("clients.id", ondelete="CASCADE"), nullable=False)
    name:      Mapped[str]  = mapped_column(String(100), nullable=False)
    position:  Mapped[int]  = mapped_column(Integer, nullable=False)
    is_won:    Mapped[bool] = mapped_column(default=False)
    is_lost:   Mapped[bool] = mapped_column(default=False)

    client: Mapped["Client"] = relationship(back_populates="pipeline_stages")

    def __repr__(self) -> str:
        return f"<PipelineStage id={self.id} name={self.name!r} pos={self.position}>"


class PromptTemplate(Base):
    """System-wide prompt template presets that any client can load."""
    __tablename__ = "prompt_templates"

    id:           Mapped[int]      = mapped_column(Integer, primary_key=True)
    slug:         Mapped[str]      = mapped_column(String(100), unique=True, nullable=False)
    niche:        Mapped[str]      = mapped_column(String(100), nullable=False)
    display_name: Mapped[str]      = mapped_column(String(255), nullable=False)
    body:         Mapped[str]      = mapped_column(Text, nullable=False)
    is_default:   Mapped[bool]     = mapped_column(Boolean, default=False)
    created_at:   Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)

    def __repr__(self) -> str:
        return f"<PromptTemplate id={self.id} slug={self.slug!r}>"


class Document(Base):
    """
    RAG knowledge base chunk. Each row is one chunk of an uploaded document,
    with its 768-dim embedding from gemini-embedding-001.
    """
    __tablename__ = "documents"

    id:          Mapped[int]      = mapped_column(Integer, primary_key=True)
    client_id:   Mapped[int]      = mapped_column(ForeignKey("clients.id", ondelete="CASCADE"), nullable=False)
    filename:    Mapped[str]      = mapped_column(String(500), nullable=False)
    chunk_index: Mapped[int]      = mapped_column(Integer, nullable=False)
    content:     Mapped[str]      = mapped_column(Text, nullable=False)
    embedding                     = mapped_column(Vector(768), nullable=True)
    created_at:  Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)

    client: Mapped["Client"] = relationship()

    def __repr__(self) -> str:
        return f"<Document id={self.id} file={self.filename!r} chunk={self.chunk_index}>"


class UsageEvent(Base):
    __tablename__ = "usage_events"

    id:            Mapped[int]      = mapped_column(Integer, primary_key=True)
    client_id:     Mapped[int]      = mapped_column(ForeignKey("clients.id", ondelete="CASCADE"), nullable=False)
    event_type:    Mapped[str]      = mapped_column(String(50), nullable=False)
    tokens_used:   Mapped[int]      = mapped_column(Integer, nullable=False, default=0)
    cost_estimate: Mapped[float]    = mapped_column(Float, nullable=False, default=0.0)
    created_at:    Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)

    client: Mapped["Client"] = relationship()

    def __repr__(self) -> str:
        return f"<UsageEvent id={self.id} type={self.event_type!r} tokens={self.tokens_used}>"


# Sprint 10: composite for monthly-usage aggregation in usage.py —
# "a client's events within the current billing window" (client_id + a
# created_at range scan). Also covers client_id-only lookups.
Index("idx_usage_events_client_created", UsageEvent.client_id, UsageEvent.created_at)


class EmailSuppression(Base):
    """
    Phase E1 — tenant-scoped do-not-email list.

    reason: unsubscribed | bounce | complaint
    Checked before every outbound send (E2). Unique per (client_id, email).
    """
    __tablename__ = "email_suppressions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    client_id: Mapped[int] = mapped_column(
        ForeignKey("clients.id", ondelete="CASCADE"), nullable=False
    )
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    reason: Mapped[str] = mapped_column(String(30), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=datetime.utcnow
    )

    client: Mapped["Client"] = relationship(back_populates="email_suppressions")

    __table_args__ = (
        UniqueConstraint("client_id", "email", name="uq_email_suppressions_client_email"),
        Index("idx_email_suppressions_client_email", "client_id", "email"),
    )

    def __repr__(self) -> str:
        return (
            f"<EmailSuppression id={self.id} client_id={self.client_id} "
            f"email={self.email!r} reason={self.reason!r}>"
        )


class EmailCampaign(Base):
    """
    Phase E7 — multi-step email sequence owned by a tenant.

    status: draft | active | paused | archived
    """
    __tablename__ = "email_campaigns"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    client_id: Mapped[int] = mapped_column(
        ForeignKey("clients.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), default="draft", server_default="draft", nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=datetime.utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow
    )

    steps: Mapped[list["EmailCampaignStep"]] = relationship(
        back_populates="campaign",
        cascade="all, delete-orphan",
        order_by="EmailCampaignStep.position",
    )
    enrollments: Mapped[list["EmailCampaignEnrollment"]] = relationship(
        back_populates="campaign",
        cascade="all, delete-orphan",
    )

    def __repr__(self) -> str:
        return f"<EmailCampaign id={self.id} name={self.name!r} status={self.status!r}>"


class EmailCampaignStep(Base):
    """One step in a campaign sequence (email template + delay from previous)."""
    __tablename__ = "email_campaign_steps"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    campaign_id: Mapped[int] = mapped_column(
        ForeignKey("email_campaigns.id", ondelete="CASCADE"), nullable=False
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False)  # 0-based order
    delay_hours: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    subject_template: Mapped[str] = mapped_column(String(500), nullable=False)
    body_template: Mapped[str] = mapped_column(Text, nullable=False)

    campaign: Mapped["EmailCampaign"] = relationship(back_populates="steps")

    __table_args__ = (
        UniqueConstraint("campaign_id", "position", name="uq_email_campaign_steps_pos"),
        Index("idx_email_campaign_steps_campaign", "campaign_id"),
    )

    def __repr__(self) -> str:
        return f"<EmailCampaignStep id={self.id} campaign={self.campaign_id} pos={self.position}>"


class EmailCampaignEnrollment(Base):
    """
    A lead enrolled in a campaign.

    status: active | paused | completed | stopped
    stop_reason: reply | unsubscribed | bounce | complaint | booked | lost |
                 takeover | suppressed | manual | campaign_paused | error
    current_step: next step position to send (0 = first step)
    """
    __tablename__ = "email_campaign_enrollments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    campaign_id: Mapped[int] = mapped_column(
        ForeignKey("email_campaigns.id", ondelete="CASCADE"), nullable=False
    )
    lead_id: Mapped[int] = mapped_column(
        ForeignKey("leads.id", ondelete="CASCADE"), nullable=False
    )
    client_id: Mapped[int] = mapped_column(
        ForeignKey("clients.id", ondelete="CASCADE"), nullable=False
    )
    status: Mapped[str] = mapped_column(
        String(20), default="active", server_default="active", nullable=False
    )
    current_step: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    delivery_run_id: Mapped[str] = mapped_column(String(64), nullable=False)
    next_run_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    stop_reason: Mapped[str | None] = mapped_column(String(40), nullable=True)
    last_sent_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    enrolled_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=datetime.utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow
    )

    campaign: Mapped["EmailCampaign"] = relationship(back_populates="enrollments")

    __table_args__ = (
        UniqueConstraint(
            "campaign_id", "lead_id", name="uq_email_campaign_enrollments_campaign_lead"
        ),
        Index("idx_email_enrollments_due", "status", "next_run_at"),
        Index("idx_email_enrollments_client", "client_id"),
    )

    def __repr__(self) -> str:
        return (
            f"<EmailCampaignEnrollment id={self.id} campaign={self.campaign_id} "
            f"lead={self.lead_id} status={self.status!r}>"
        )


class EmailCampaignDeliveryAttempt(Base):
    """Durable identity and state for one campaign enrollment step send."""
    __tablename__ = "email_campaign_delivery_attempts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    enrollment_id: Mapped[int] = mapped_column(
        ForeignKey("email_campaign_enrollments.id", ondelete="CASCADE"),
        nullable=False,
    )
    campaign_id: Mapped[int] = mapped_column(
        ForeignKey("email_campaigns.id", ondelete="CASCADE"),
        nullable=False,
    )
    client_id: Mapped[int] = mapped_column(
        ForeignKey("clients.id", ondelete="CASCADE"),
        nullable=False,
    )
    delivery_run_id: Mapped[str] = mapped_column(String(64), nullable=False)
    step_position: Mapped[int] = mapped_column(Integer, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    state: Mapped[str] = mapped_column(
        String(20), default="pending", server_default="pending", nullable=False
    )
    attempt_count: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    provider_message_id: Mapped[str | None] = mapped_column(
        String(255), nullable=True
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=datetime.utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow
    )
    sent_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        UniqueConstraint(
            "campaign_id",
            "enrollment_id",
            "delivery_run_id",
            "step_position",
            name="uq_email_delivery_attempt_execution",
        ),
        UniqueConstraint(
            "idempotency_key",
            name="uq_email_delivery_attempt_idempotency_key",
        ),
        CheckConstraint(
            "state IN ('pending', 'sending', 'sent', 'failed')",
            name="ck_email_delivery_attempt_state",
        ),
        Index(
            "idx_email_delivery_attempt_enrollment",
            "enrollment_id",
            "delivery_run_id",
        ),
        Index("idx_email_delivery_attempt_client", "client_id"),
    )


class DailyStat(Base):
    """
    Sprint 7 — nightly analytics rollup.

    One row per (client_id, date). `stats` is a JSONB blob holding the
    aggregated metrics produced by analytics.rollup_daily_stats() so the
    schema can grow new KPIs without a migration each time.

    The (client_id, date) uniqueness lets the rollup job UPSERT — re-running
    it for the same day overwrites rather than duplicates.
    """
    __tablename__ = "daily_stats"

    id:        Mapped[int]           = mapped_column(Integer, primary_key=True)
    client_id: Mapped[int]           = mapped_column(ForeignKey("clients.id", ondelete="CASCADE"), nullable=False)
    date:      Mapped[date_type]     = mapped_column(Date, nullable=False)
    stats:     Mapped[dict]          = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime]     = mapped_column(DateTime(timezone=True), default=datetime.utcnow)

    client: Mapped["Client"] = relationship()

    __table_args__ = (
        UniqueConstraint("client_id", "date", name="uq_daily_stats_client_date"),
        Index("idx_daily_stats_client_date", "client_id", "date"),
    )

    def __repr__(self) -> str:
        return f"<DailyStat id={self.id} client_id={self.client_id} date={self.date}>"
