from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from geoalchemy2 import Geography
from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    email: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    provider_customer_id: Mapped[str | None] = mapped_column(Text, unique=True)
    ab_cohort: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'A'"))
    last_login: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    join_research_aggregated_success_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    remaining_quota: Mapped[int | None] = mapped_column(
        Integer,
        CheckConstraint(
            "remaining_quota IS NULL OR remaining_quota >= 0",
            name="ck_users_remaining_quota_non_negative",
        ),
        nullable=True,
    )


class ConstructionQuery(Base):
    __tablename__ = "construction_queries"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE", name="construction_queries_user_id_fkey"),
        nullable=False,
    )
    fingerprint: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint("user_id", "fingerprint", name="uq_construction_queries_user_fingerprint"),
        Index("ix_construction_queries_user_id", "user_id"),
    )


class BillingPlan(Base):
    __tablename__ = "billing_plans"

    code: Mapped[str] = mapped_column(Text, primary_key=True)
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    display_price: Mapped[str] = mapped_column(Text, nullable=False)
    amount_usd_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    duration_days: Mapped[int] = mapped_column(Integer, nullable=False)
    daily_limit: Mapped[int] = mapped_column(Integer, nullable=False)
    currency: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'USD'"))
    dodo_product_id: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        CheckConstraint("amount_usd_cents > 0", name="billing_plans_amount_usd_cents_check"),
        CheckConstraint("duration_days > 0", name="billing_plans_duration_days_check"),
        CheckConstraint("daily_limit > 0", name="billing_plans_daily_limit_check"),
    )


class POI(Base):
    __tablename__ = "pois"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[str | None] = mapped_column(Text)
    geom: Mapped[Any] = mapped_column(Geography(geometry_type="POINT", srid=4326), nullable=False)
    source: Mapped[str | None] = mapped_column(Text)
    confidence: Mapped[int | None] = mapped_column(SmallInteger)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (Index("idx_pois_geom", "geom", postgresql_using="gist"),)


class PaymentIntent(Base):
    __tablename__ = "payment_intents"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE", name="payment_intents_user_id_fkey"),
        nullable=False,
    )
    plan_code: Mapped[str] = mapped_column(
        Text,
        ForeignKey("billing_plans.code", name="payment_intents_plan_code_fkey"),
        nullable=False,
    )
    amount_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    provider_intent_id: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'pending'"))
    currency: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'USD'"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        UniqueConstraint("provider_intent_id", name="payment_intents_provider_intent_id_key"),
        Index("idx_payment_intents_provider_intent_id", "provider_intent_id", unique=True),
        Index("idx_payment_intents_user_created_at", "user_id", "created_at"),
    )


class UserPass(Base):
    __tablename__ = "user_passes"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE", name="user_passes_user_id_fkey"),
        nullable=False,
    )
    plan_code: Mapped[str] = mapped_column(
        Text,
        ForeignKey("billing_plans.code", name="user_passes_plan_code_fkey"),
        nullable=False,
    )
    provider_payment_id: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    amount_paid_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'active'"))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        UniqueConstraint("provider_payment_id", name="user_passes_provider_payment_id_key"),
        Index("idx_user_passes_user_id", "user_id"),
    )


class UGCReport(Base):
    __tablename__ = "ugc_reports"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    reporter_anon_id: Mapped[str] = mapped_column(Text, nullable=False)
    reporter_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
    reporter_tier: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'free'"))
    title: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[str | None] = mapped_column(Text)
    noise_type: Mapped[str | None] = mapped_column(Text)
    severity: Mapped[int | None] = mapped_column(SmallInteger)
    occurred_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    geom: Mapped[Any] = mapped_column(Geography(geometry_type="POINT", srid=4326), nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'pending'"))
    moderator_note: Mapped[str | None] = mapped_column(Text)
    moderated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    content_hash: Mapped[str] = mapped_column(Text, nullable=False)
    geo_cell: Mapped[str] = mapped_column(Text, nullable=False)
    day_bucket: Mapped[str] = mapped_column(Text, nullable=False)
    duplicate_of_id: Mapped[int | None] = mapped_column(ForeignKey("ugc_reports.id", name="ugc_reports_duplicate_of_id_fkey"))
    nearest_poi_id: Mapped[int | None] = mapped_column(ForeignKey("pois.id", name="ugc_reports_nearest_poi_id_fkey"))
    nearest_poi_distance_m: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    public_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, unique=True, server_default=text("gen_random_uuid()"))

    __table_args__ = (
        CheckConstraint("char_length(category) <= 50", name="ugc_reports_category_check"),
        CheckConstraint("char_length(noise_type) <= 50", name="ugc_reports_noise_type_check"),
        CheckConstraint("severity >= 1 AND severity <= 5", name="ugc_reports_severity_check"),
        UniqueConstraint("content_hash", "geo_cell", "day_bucket", name="ugc_reports_dedup_unique"),
        Index("ugc_reports_content_hash", "content_hash"),
        Index("ugc_reports_geo_cell_created_at", "geo_cell", "created_at"),
        Index("ugc_reports_geom_gist", "geom", postgresql_using="gist"),
        Index("ugc_reports_reporter_anon_created_at", "reporter_anon_id", "created_at"),
        Index("ugc_reports_status_created_at", "status", "created_at"),
        Index("ugc_reports_status_occurred_at", "status", "occurred_at"),
        Index("ugc_reports_reporter_user_created_at", "reporter_user_id", "created_at"),
    )


class UGCReportEvidence(Base):
    __tablename__ = "ugc_report_evidence"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    report_id: Mapped[int] = mapped_column(ForeignKey("ugc_reports.id", ondelete="CASCADE"), nullable=False)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    url_hash: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        CheckConstraint("char_length(url) <= 2000", name="ugc_report_evidence_url_check"),
        UniqueConstraint("report_id", "url_hash", name="ugc_report_evidence_unique_per_report"),
        Index("ugc_report_evidence_report_id", "report_id"),
    )


class UserLocationQuery(Base):
    __tablename__ = "user_location_queries"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    anon_id: Mapped[str | None] = mapped_column(Text)
    session_id: Mapped[str | None] = mapped_column(Text)
    user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
    input_kind: Mapped[str] = mapped_column(Text, nullable=False)
    original_input: Mapped[str] = mapped_column(Text, nullable=False)
    source_url: Mapped[str | None] = mapped_column(Text)
    normalized_input: Mapped[str] = mapped_column(Text, nullable=False)
    latitude: Mapped[float] = mapped_column(nullable=False)
    longitude: Mapped[float] = mapped_column(nullable=False)
    location: Mapped[Any] = mapped_column(Geography(geometry_type="POINT", srid=4326), nullable=False)
    demand_cell_id: Mapped[str | None] = mapped_column(Text)
    request_country: Mapped[str | None] = mapped_column(Text)
    request_city: Mapped[str | None] = mapped_column(Text)
    user_agent: Mapped[str | None] = mapped_column(Text)
    result_status: Mapped[str] = mapped_column(Text, nullable=False)
    result_count: Mapped[int | None] = mapped_column(Integer)
    error_code: Mapped[str | None] = mapped_column(Text)
    response_ms: Mapped[int | None] = mapped_column(Integer)
    is_duplicate_window: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))

    __table_args__ = (
        Index("idx_user_location_queries_anon_created", "anon_id", "created_at"),
        Index("idx_user_location_queries_created_at", "created_at"),
        Index("idx_user_location_queries_demand_cell_created", "demand_cell_id", "created_at"),
        Index("idx_user_location_queries_location", "location", postgresql_using="gist"),
        Index("idx_user_location_queries_session_created", "session_id", "created_at"),
        Index("idx_user_location_queries_user_created", "user_id", "created_at"),
    )


class MagicLinkToken(Base):
    __tablename__ = "magic_link_tokens"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", name="magic_link_tokens_user_id_fkey"))
    email: Mapped[str] = mapped_column(Text, nullable=False)
    token_hash: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    purpose: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'login'"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    redeemed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    requester_ip_hash: Mapped[str | None] = mapped_column(Text)
    request_ip: Mapped[str | None] = mapped_column(Text)
    user_agent_hash: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        UniqueConstraint("token_hash", name="magic_link_tokens_token_hash_key"),
        Index("idx_magic_tokens_email_created", "email", "created_at"),
        Index("idx_magic_tokens_expires_at", "expires_at"),
        Index("idx_magic_tokens_redeemed_at", "redeemed_at"),
    )


class FeatureFlag(Base):
    __tablename__ = "feature_flags"

    key: Mapped[str] = mapped_column(Text, primary_key=True)
    is_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False)
    config: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_by: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        Index("idx_feature_flags_updated_at", "updated_at"),
    )


class SimulatedBillingPlan(Base):
    __tablename__ = "simulated_billing_plans"

    code: Mapped[str] = mapped_column(Text, primary_key=True)
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    duration_hours: Mapped[int] = mapped_column(Integer, nullable=False)
    daily_limit: Mapped[int] = mapped_column(Integer, nullable=False)
    cohort: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        CheckConstraint("duration_hours > 0", name="simulated_billing_plans_duration_hours_check"),
        CheckConstraint("daily_limit > 0", name="simulated_billing_plans_daily_limit_check"),
        Index("idx_simulated_billing_plans_is_active", "is_active"),
        Index("idx_simulated_billing_plans_cohort", "cohort"),
    )


class SimulatedPaymentIntent(Base):
    __tablename__ = "simulated_payment_intents"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE", name="simulated_payment_intents_user_id_fkey"),
        nullable=False,
    )
    plan_code: Mapped[str] = mapped_column(
        Text,
        ForeignKey("simulated_billing_plans.code", name="simulated_payment_intents_plan_code_fkey"),
        nullable=False,
    )
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'initiated'"))
    source: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'simulated_paid'"))
    upgraded_from_anon_id: Mapped[str | None] = mapped_column(Text)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoke_reason: Mapped[str | None] = mapped_column(Text)
    requester_ip_hash: Mapped[str | None] = mapped_column(Text)
    user_agent_hash: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        CheckConstraint(
            "status IN ('initiated', 'magic_sent', 'activated', 'expired', 'revoked')",
            name="simulated_payment_intents_status_check",
        ),
        CheckConstraint("source = 'simulated_paid'", name="simulated_payment_intents_source_check"),
        Index("idx_simulated_payment_intents_user_created", "user_id", "created_at"),
        Index("idx_simulated_payment_intents_status_created", "status", "created_at"),
        Index("idx_simulated_payment_intents_plan_code", "plan_code"),
        Index("idx_simulated_payment_intents_created_at", "created_at"),
    )


class SimulatedUserPass(Base):
    __tablename__ = "simulated_user_passes"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE", name="simulated_user_passes_user_id_fkey"),
        nullable=False,
    )
    plan_code: Mapped[str] = mapped_column(
        Text,
        ForeignKey("simulated_billing_plans.code", name="simulated_user_passes_plan_code_fkey"),
        nullable=False,
    )
    simulated_intent_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(
            "simulated_payment_intents.id",
            ondelete="CASCADE",
            name="simulated_user_passes_simulated_intent_id_fkey",
        ),
        nullable=False,
    )
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'active'"))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoke_reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        CheckConstraint("status IN ('active','expired','revoked')", name="simulated_user_passes_status_check"),
        Index("idx_simulated_user_passes_user_expires", "user_id", "expires_at"),
        Index("idx_simulated_user_passes_status_expires", "status", "expires_at"),
        Index("idx_simulated_user_passes_intent", "simulated_intent_id"),
        Index("idx_simulated_user_passes_updated_at", "updated_at"),
    )


class FunnelEvent(Base):
    __tablename__ = "funnel_events"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    event_name: Mapped[str] = mapped_column(Text, nullable=False)
    event_source: Mapped[str] = mapped_column(Text, nullable=False)
    event_version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    anon_id: Mapped[str | None] = mapped_column(Text)
    session_id: Mapped[str | None] = mapped_column(Text)
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL", name="funnel_events_user_id_fkey"),
    )
    effective_tier: Mapped[str] = mapped_column(Text, nullable=False)
    target_tier: Mapped[str | None] = mapped_column(Text)
    transition_name: Mapped[str | None] = mapped_column(Text)
    related_query_id: Mapped[int | None] = mapped_column(
        ForeignKey("user_location_queries.id", ondelete="SET NULL", name="funnel_events_related_query_id_fkey")
    )
    related_simulated_intent_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(
            "simulated_payment_intents.id",
            ondelete="SET NULL",
            name="funnel_events_related_simulated_intent_id_fkey",
        ),
    )
    related_simulated_pass_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(
            "simulated_user_passes.id",
            ondelete="SET NULL",
            name="funnel_events_related_simulated_pass_id_fkey",
        ),
    )
    related_payment_intent_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("payment_intents.id", ondelete="SET NULL", name="funnel_events_related_payment_intent_id_fkey"),
    )
    related_user_pass_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("user_passes.id", ondelete="SET NULL", name="funnel_events_related_user_pass_id_fkey"),
    )
    ui_surface: Mapped[str | None] = mapped_column(Text)
    check_type: Mapped[str | None] = mapped_column(Text)
    cohort: Mapped[str | None] = mapped_column(Text)
    selected_language: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'en'"))
    metadata_json: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )

    __table_args__ = (
        CheckConstraint("effective_tier IN ('free', 'simulated_paid', 'paid_1_day', 'paid_3_days')", name="funnel_events_effective_tier_check"),
        CheckConstraint(
            "target_tier IS NULL OR target_tier IN ('simulated_paid', 'paid_1_day', 'paid_3_days')",
            name="funnel_events_target_tier_check",
        ),
        CheckConstraint(
            "transition_name IS NULL OR transition_name IN ('free_to_simulated_paid', 'free_to_paid_1_day', 'free_to_paid_3_days')",
            name="funnel_events_transition_name_check",
        ),
        CheckConstraint(
            "ui_surface IS NULL OR ui_surface IN ('construction_level_page', 'demand_level_page', 'hero_unlock_button', 'user_access_modal', 'share_modal', 'about_modal')",
            name="funnel_events_ui_surface_check",
        ),
        CheckConstraint("check_type IS NULL OR check_type IN ('construction', 'demand')", name="funnel_events_check_type_check"),
        CheckConstraint("event_version > 0", name="funnel_events_event_version_check"),
        CheckConstraint(
            "event_name <> 'check_completed' OR related_query_id IS NOT NULL",
            name="funnel_events_check_completed_requires_query",
        ),
        CheckConstraint(
            "NOT (event_name = 'check_completed' AND check_type = 'demand' AND effective_tier NOT IN ('simulated_paid', 'paid_1_day', 'paid_3_days'))",
            name="funnel_events_demand_completion_tier_check",
        ),
        Index("idx_funnel_events_created_at", "created_at"),
        Index("idx_funnel_events_event_created", "event_name", "created_at"),
        Index("idx_funnel_events_user_created", "user_id", "created_at"),
        Index("idx_funnel_events_anon_created", "anon_id", "created_at"),
        Index("idx_funnel_events_session_created", "session_id", "created_at"),
    )


class CellPoiPrecompute(Base):
    __tablename__ = "cell_poi_precompute"

    cell_id: Mapped[str] = mapped_column(Text, primary_key=True)
    data: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class WebhookEvent(Base):
    __tablename__ = "webhook_events"

    provider: Mapped[str] = mapped_column(Text, primary_key=True)
    event_id: Mapped[str] = mapped_column(Text, primary_key=True)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'received'"))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)

    __table_args__ = (
        CheckConstraint("status IN ('received', 'processed', 'failed')", name="webhook_events_status_check"),
    )


class FreeQuota(Base):
    __tablename__ = "free_quotas"

    cohort: Mapped[str] = mapped_column(Text, primary_key=True)
    daily_limit: Mapped[int] = mapped_column(Integer, nullable=False)

    __table_args__ = (
        CheckConstraint("daily_limit > 0", name="free_quotas_daily_limit_check"),
    )
