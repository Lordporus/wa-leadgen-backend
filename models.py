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

from datetime import datetime
from sqlalchemy import (
    Integer, String, Text, DateTime, ForeignKey, Index
)
from sqlalchemy.orm import relationship, Mapped, mapped_column
from database import Base


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

    # ── F6b: multi-tenant scheduler jobs ──────────────────────────────────
    admin_phone: Mapped[str | None] = mapped_column(String(50), nullable=True)
    # TODO: Before actively using this field for per-client API calls,
    # it must be encrypted at rest (e.g. via Supabase Vault or
    # application-level encryption). Do not store or use real per-client
    # tokens in plaintext in production.
    calendly_api_token: Mapped[str | None] = mapped_column(String(255), nullable=True)

    leads: Mapped[list["Lead"]] = relationship(back_populates="client")
    pipeline_stages: Mapped[list["PipelineStage"]] = relationship(
        back_populates="client", order_by="PipelineStage.position"
    )

    def __repr__(self) -> str:
        return f"<Client id={self.id} name={self.name!r}>"


class Lead(Base):
    """
    Single source of truth for a prospect. `phone` is the unique lookup key,
    stored without `+`/spaces — matching the Airtable convention
    (see docs/schema.md: "Phone number type").
    """
    __tablename__ = "leads"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    phone: Mapped[str] = mapped_column(String(20), unique=True, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(255), default="WhatsApp User")
    source: Mapped[str | None] = mapped_column(String(100), nullable=True)
    status: Mapped[str] = mapped_column(String(50), default="New Lead", index=True)
    business_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    lead_score: Mapped[str | None] = mapped_column(String(20), nullable=True)
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


class Message(Base):
    """Append-only conversation log. Replaces the Airtable long-text field."""
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
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=datetime.utcnow
    )

    lead: Mapped["Lead"] = relationship(back_populates="messages")

    def __repr__(self) -> str:
        return f"<Message id={self.id} lead_id={self.lead_id} dir={self.direction!r}>"


# Convenient composite index for the follow-up job's "lead by status" queries.
Index("idx_messages_lead_id", Message.lead_id)


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
