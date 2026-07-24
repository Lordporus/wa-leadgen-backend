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

from datetime import datetime, date as date_type
from sqlalchemy import (
    Integer, String, Text, DateTime, Date, ForeignKey, Index, Boolean, Float,
    UniqueConstraint, text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import relationship, Mapped, mapped_column
from pgvector.sqlalchemy import Vector
from app.core.database import Base


class Client(Base):
    """
    Agency tenant.
    Phase 7: single default client (id=1).
    Phase 8: one row per client — holds per-client Gemini prompt, WhatsApp
             phone number ID, Calendly link and follow-up template.
    """
    __tablename__ = "clients"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)

    # ── Phase 8: per-client config ────────────────────────────────────────
    wa_phone_number_id: Mapped[str | None] = mapped_column(String(50),  nullable=True)
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
    Single source of truth for a prospect. `phone` is the unique lookup key,
    stored without `+`/spaces — matching the Airtable convention
    (see docs/schema.md: "Phone number type").

    Email (Phase E1): optional secondary contact. Phone remains required.
    Uniqueness is tenant-scoped: one email per client when email is set.
    """
    __tablename__ = "leads"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    phone: Mapped[str] = mapped_column(String(20), unique=True, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(255), default="WhatsApp User")
    source: Mapped[str | None] = mapped_column(String(100), nullable=True)
    status: Mapped[str] = mapped_column(String(50), default="New Lead", index=True)
    business_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    lead_score: Mapped[str | None] = mapped_column(String(20), nullable=True)
    lead_score_numeric: Mapped[int | None] = mapped_column(Integer, nullable=True)
    notified_hot_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    is_human_takeover: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
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
