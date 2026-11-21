"""Relational model for Crowl's catalog, crawl ledger and published history.

The model deliberately uses portable SQLAlchemy types so the same schema can be
exercised with SQLite in unit tests. PostgreSQL receives JSONB through a type
variant and timezone-aware timestamps in production.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Enum as SAEnum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    false,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base
from .enums import (
    Availability,
    BatchStatus,
    Direction,
    ExecutionCoverage,
    ExtractorRunStatus,
    LinkRelationship,
    PriceCompleteness,
    PricingSemantics,
    ProtectionState,
    SearchOutcome,
    TerminalKind,
    TransportMode,
)


JSON_VALUE = JSON().with_variant(JSONB(), "postgresql")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def enum_type(enum: type, name: str) -> SAEnum:
    return SAEnum(
        enum,
        name=name,
        native_enum=False,
        validate_strings=True,
        values_callable=lambda values: [value.value for value in values],
    )


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )


class City(TimestampMixin, Base):
    __tablename__ = "cities"

    id: Mapped[str] = mapped_column(String(96), primary_key=True)
    slug: Mapped[str] = mapped_column(String(96), unique=True, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    country_code: Mapped[str] = mapped_column(String(2), nullable=False, index=True)
    country_name: Mapped[str] = mapped_column(String(96), nullable=False)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False)
    latitude: Mapped[float] = mapped_column(Float, nullable=False)
    longitude: Mapped[float] = mapped_column(Float, nullable=False)
    visited: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    wishlist: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    categories: Mapped[list[str]] = mapped_column(JSON_VALUE, default=list, nullable=False)
    attributes: Mapped[dict[str, int]] = mapped_column(JSON_VALUE, default=dict, nullable=False)
    summary: Mapped[str] = mapped_column(Text, default="", nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False, index=True)

    photos: Mapped[list["CityPhoto"]] = relationship(
        back_populates="city", cascade="all, delete-orphan"
    )

    __table_args__ = (
        CheckConstraint("latitude >= -90 AND latitude <= 90", name="latitude_range"),
        CheckConstraint("longitude >= -180 AND longitude <= 180", name="longitude_range"),
    )


class CityPhoto(TimestampMixin, Base):
    __tablename__ = "city_photos"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    city_id: Mapped[str] = mapped_column(
        ForeignKey("cities.id", ondelete="CASCADE"), nullable=False, index=True
    )
    asset_path: Mapped[str] = mapped_column(String(512), nullable=False)
    alt_text: Mapped[str] = mapped_column(String(500), nullable=False)
    creator: Mapped[str | None] = mapped_column(String(200))
    source_url: Mapped[str | None] = mapped_column(String(1000))
    license: Mapped[str | None] = mapped_column(String(160))
    attribution: Mapped[str | None] = mapped_column(String(500))
    object_position: Mapped[str] = mapped_column(String(32), default="50% 50%", nullable=False)
    curation_status: Mapped[str] = mapped_column(String(32), default="pending", nullable=False)
    is_primary: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    city: Mapped[City] = relationship(back_populates="photos")

    __table_args__ = (
        UniqueConstraint("city_id", "asset_path", name="uq_city_photos_city_asset"),
    )


class Terminal(TimestampMixin, Base):
    __tablename__ = "terminals"

    id: Mapped[str] = mapped_column(String(96), primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    kind: Mapped[TerminalKind] = mapped_column(
        enum_type(TerminalKind, "terminal_kind"), nullable=False
    )
    country_code: Mapped[str] = mapped_column(String(2), nullable=False, index=True)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False)
    latitude: Mapped[float] = mapped_column(Float, nullable=False)
    longitude: Mapped[float] = mapped_column(Float, nullable=False)
    schengen_eligible: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    __table_args__ = (
        CheckConstraint("latitude >= -90 AND latitude <= 90", name="terminal_latitude_range"),
        CheckConstraint(
            "longitude >= -180 AND longitude <= 180", name="terminal_longitude_range"
        ),
    )


class CityTerminalLink(TimestampMixin, Base):
    __tablename__ = "city_terminal_links"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    city_id: Mapped[str] = mapped_column(
        ForeignKey("cities.id", ondelete="CASCADE"), nullable=False
    )
    terminal_id: Mapped[str] = mapped_column(
        ForeignKey("terminals.id", ondelete="CASCADE"), nullable=False
    )
    relationship_type: Mapped[LinkRelationship] = mapped_column(
        enum_type(LinkRelationship, "city_terminal_relationship"), nullable=False
    )

    __table_args__ = (
        UniqueConstraint("city_id", "terminal_id", name="uq_city_terminal_links_pair"),
        Index("ix_city_terminal_links_terminal_city", "terminal_id", "city_id"),
    )


class RuleVersion(TimestampMixin, Base):
    __tablename__ = "rule_versions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    config_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    version_label: Mapped[str] = mapped_column(String(64), nullable=False)
    schengen_scope_version: Mapped[str] = mapped_column(String(64), nullable=False)
    strict_schengen_path: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    rules: Mapped[dict[str, Any]] = mapped_column(JSON_VALUE, default=dict, nullable=False)


class OriginTerminal(Base):
    __tablename__ = "origin_terminals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    rule_version_id: Mapped[int] = mapped_column(
        ForeignKey("rule_versions.id", ondelete="CASCADE"), nullable=False
    )
    terminal_id: Mapped[str] = mapped_column(
        ForeignKey("terminals.id", ondelete="RESTRICT"), nullable=False
    )
    permits_departure: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    permits_return: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    __table_args__ = (
        UniqueConstraint("rule_version_id", "terminal_id", name="uq_origin_terminals_version_terminal"),
    )


class TerminalTransfer(TimestampMixin, Base):
    __tablename__ = "terminal_transfers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    from_terminal_id: Mapped[str] = mapped_column(ForeignKey("terminals.id"), nullable=False)
    to_terminal_id: Mapped[str] = mapped_column(ForeignKey("terminals.id"), nullable=False)
    minimum_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    cost_eur_cents: Mapped[int | None] = mapped_column(Integer)
    cost_is_complete: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    evidence_url: Mapped[str | None] = mapped_column(String(1000))

    __table_args__ = (
        UniqueConstraint("from_terminal_id", "to_terminal_id", name="uq_terminal_transfers_edge"),
        CheckConstraint("minimum_minutes >= 0", name="minimum_minutes_nonnegative"),
        CheckConstraint("cost_eur_cents IS NULL OR cost_eur_cents >= 0", name="transfer_cost_nonnegative"),
    )


class Source(TimestampMixin, Base):
    __tablename__ = "sources"

    slug: Mapped[str] = mapped_column(String(80), primary_key=True)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    supported_modes: Mapped[list[str]] = mapped_column(JSON_VALUE, default=list, nullable=False)
    strategy: Mapped[str] = mapped_column(String(40), nullable=False)
    is_requested: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False, index=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)
    disabled_reason: Mapped[str | None] = mapped_column(String(500))
    connector_version: Mapped[str] = mapped_column(String(64), default="planned", nullable=False)
    parser_version: Mapped[str] = mapped_column(String(64), default="planned", nullable=False)


class SourceTerminalAlias(TimestampMixin, Base):
    __tablename__ = "source_terminal_aliases"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source_id: Mapped[str] = mapped_column(ForeignKey("sources.slug"), nullable=False)
    terminal_id: Mapped[str] = mapped_column(ForeignKey("terminals.id"), nullable=False)
    alias: Mapped[str] = mapped_column(String(160), nullable=False)
    provenance: Mapped[str | None] = mapped_column(String(500))
    effective_from: Mapped[date | None] = mapped_column(Date)
    effective_to: Mapped[date | None] = mapped_column(Date)

    __table_args__ = (
        UniqueConstraint("source_id", "alias", name="uq_source_terminal_aliases_source_alias"),
    )


class SourceRouteCoverage(TimestampMixin, Base):
    __tablename__ = "source_route_coverage"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source_id: Mapped[str] = mapped_column(ForeignKey("sources.slug"), nullable=False)
    origin_terminal_id: Mapped[str] = mapped_column(ForeignKey("terminals.id"), nullable=False)
    destination_terminal_id: Mapped[str] = mapped_column(ForeignKey("terminals.id"), nullable=False)
    effective_from: Mapped[date | None] = mapped_column(Date)
    effective_to: Mapped[date | None] = mapped_column(Date)
    discovered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    provenance: Mapped[str | None] = mapped_column(String(500))

    __table_args__ = (
        UniqueConstraint(
            "source_id",
            "origin_terminal_id",
            "destination_terminal_id",
            "effective_from",
            name="uq_source_route_coverage_route_effective",
        ),
    )


class SearchProfile(TimestampMixin, Base):
    __tablename__ = "search_profiles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    config_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    passenger_age: Mapped[int] = mapped_column(Integer, default=22, nullable=False)
    student: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), default="EUR", nullable=False)
    baggage: Mapped[str] = mapped_column(String(64), default="smallest_included", nullable=False)
    entitlements: Mapped[dict[str, Any]] = mapped_column(JSON_VALUE, default=dict, nullable=False)

    __table_args__ = (
        CheckConstraint("passenger_age >= 0", name="passenger_age_nonnegative"),
    )


class TravelWindow(TimestampMixin, Base):
    __tablename__ = "travel_windows"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    friday_date: Mapped[date] = mapped_column(Date, unique=True, nullable=False, index=True)
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    timezone: Mapped[str] = mapped_column(String(64), default="Europe/Paris", nullable=False)
    minimum_stay_minutes: Mapped[int] = mapped_column(Integer, default=720, nullable=False)
    maximum_direction_minutes: Mapped[int] = mapped_column(Integer, default=720, nullable=False)

    __table_args__ = (
        CheckConstraint("ends_at > starts_at", name="window_end_after_start"),
        CheckConstraint("minimum_stay_minutes >= 0", name="minimum_stay_nonnegative"),
        CheckConstraint("maximum_direction_minutes > 0", name="max_direction_positive"),
    )


class FxRate(Base):
    __tablename__ = "fx_rates"

    rate_date: Mapped[date] = mapped_column(Date, primary_key=True)
    currency: Mapped[str] = mapped_column(String(3), primary_key=True)
    eur_per_unit: Mapped[float] = mapped_column(Float, nullable=False)
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        CheckConstraint("eur_per_unit > 0", name="fx_rate_positive"),
    )


class CrawlRun(TimestampMixin, Base):
    __tablename__ = "crawl_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    scheduled_for: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    is_synthetic: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        server_default=false(),
        nullable=False,
    )
    rule_version_id: Mapped[int] = mapped_column(ForeignKey("rule_versions.id"), nullable=False)
    profile_id: Mapped[int] = mapped_column(ForeignKey("search_profiles.id"), nullable=False)
    manifest_version: Mapped[str] = mapped_column(String(64), nullable=False)
    release_version: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[BatchStatus] = mapped_column(
        enum_type(BatchStatus, "batch_status"), default=BatchStatus.QUEUED, nullable=False
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expected_cells: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    completed_cells: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    successful_cells: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    diagnostic: Mapped[str | None] = mapped_column(String(500))

    __table_args__ = (
        UniqueConstraint(
            "scheduled_for",
            "is_synthetic",
            name="uq_crawl_runs_scheduled_for_synthetic",
        ),
        CheckConstraint("expected_cells >= 0", name="crawl_expected_nonnegative"),
        CheckConstraint("completed_cells >= 0", name="crawl_completed_nonnegative"),
        CheckConstraint("successful_cells >= 0", name="crawl_successful_nonnegative"),
    )


class ExtractorRun(TimestampMixin, Base):
    __tablename__ = "extractor_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    crawl_run_id: Mapped[int] = mapped_column(ForeignKey("crawl_runs.id"), nullable=False)
    source_id: Mapped[str] = mapped_column(ForeignKey("sources.slug"), nullable=False)
    status: Mapped[ExtractorRunStatus] = mapped_column(
        enum_type(ExtractorRunStatus, "extractor_run_status"), nullable=False
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_response_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    runtime_ms: Mapped[int | None] = mapped_column(Integer)
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    expected_searches: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    attempted_searches: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    successful_searches: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    empty_searches: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    result_searches: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    physical_requests: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    offers_found: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    diagnostic: Mapped[str | None] = mapped_column(String(500))

    __table_args__ = (
        UniqueConstraint("crawl_run_id", "source_id", name="uq_extractor_runs_batch_source"),
        Index("ix_extractor_runs_batch_source_status", "crawl_run_id", "source_id", "status"),
    )


class CrawlCoverageCell(TimestampMixin, Base):
    __tablename__ = "crawl_coverage_cells"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    crawl_run_id: Mapped[int] = mapped_column(ForeignKey("crawl_runs.id"), nullable=False)
    source_id: Mapped[str] = mapped_column(ForeignKey("sources.slug"), nullable=False)
    city_id: Mapped[str] = mapped_column(ForeignKey("cities.id"), nullable=False)
    travel_window_id: Mapped[int] = mapped_column(ForeignKey("travel_windows.id"), nullable=False)
    direction: Mapped[Direction] = mapped_column(enum_type(Direction, "coverage_direction"), nullable=False)
    applicable: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    outcome: Mapped[SearchOutcome | None] = mapped_column(
        enum_type(SearchOutcome, "search_outcome")
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        UniqueConstraint(
            "crawl_run_id",
            "source_id",
            "city_id",
            "travel_window_id",
            "direction",
            name="uq_crawl_coverage_cells_logical",
        ),
    )


class ConnectorRequest(TimestampMixin, Base):
    __tablename__ = "connector_requests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    crawl_run_id: Mapped[int] = mapped_column(ForeignKey("crawl_runs.id"), nullable=False)
    source_id: Mapped[str] = mapped_column(ForeignKey("sources.slug"), nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    outcome: Mapped[SearchOutcome | None] = mapped_column(
        enum_type(SearchOutcome, "connector_request_outcome")
    )
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    safe_diagnostic: Mapped[str | None] = mapped_column(String(500))

    __table_args__ = (
        UniqueConstraint(
            "crawl_run_id", "source_id", "request_fingerprint", name="uq_connector_request_fingerprint"
        ),
        Index("ix_connector_requests_batch_source_outcome", "crawl_run_id", "source_id", "outcome"),
    )


class HealthEvent(TimestampMixin, Base):
    __tablename__ = "health_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    crawl_run_id: Mapped[int | None] = mapped_column(ForeignKey("crawl_runs.id"))
    source_id: Mapped[str | None] = mapped_column(ForeignKey("sources.slug"))
    event_type: Mapped[str] = mapped_column(String(80), nullable=False)
    severity: Mapped[str] = mapped_column(String(24), nullable=False)
    diagnostic: Mapped[str] = mapped_column(String(500), nullable=False)
    recovered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class FareSnapshot(TimestampMixin, Base):
    __tablename__ = "fare_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    observed_on: Mapped[date] = mapped_column(Date, nullable=False)
    profile_id: Mapped[int] = mapped_column(ForeignKey("search_profiles.id"), nullable=False)
    destination_city_id: Mapped[str] = mapped_column(ForeignKey("cities.id"), nullable=False)
    travel_window_id: Mapped[int] = mapped_column(ForeignKey("travel_windows.id"), nullable=False)
    mode_bucket: Mapped[TransportMode] = mapped_column(
        enum_type(TransportMode, "transport_mode"), nullable=False
    )
    is_synthetic: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        server_default=false(),
        nullable=False,
    )
    current_revision_id: Mapped[int | None] = mapped_column(
        ForeignKey(
            "fare_snapshot_revisions.id",
            name="fk_fare_snapshots_current_revision",
            use_alter=True,
        )
    )

    __table_args__ = (
        UniqueConstraint(
            "observed_on",
            "profile_id",
            "destination_city_id",
            "travel_window_id",
            "mode_bucket",
            "is_synthetic",
            name="uq_fare_snapshots_daily_cell_provenance",
        ),
        Index(
            "ix_fare_snapshots_city_window_mode_observed",
            "destination_city_id",
            "travel_window_id",
            "mode_bucket",
            "observed_on",
        ),
        Index("ix_fare_snapshots_window_observed", "travel_window_id", "observed_on"),
    )


class FareSnapshotRevision(Base):
    __tablename__ = "fare_snapshot_revisions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    fare_snapshot_id: Mapped[int] = mapped_column(
        ForeignKey("fare_snapshots.id", ondelete="CASCADE"), nullable=False
    )
    revision_number: Mapped[int] = mapped_column(Integer, nullable=False)
    crawl_run_id: Mapped[int] = mapped_column(ForeignKey("crawl_runs.id"), nullable=False)
    rule_version_id: Mapped[int] = mapped_column(ForeignKey("rule_versions.id"), nullable=False)
    profile_id: Mapped[int] = mapped_column(ForeignKey("search_profiles.id"), nullable=False)
    mode_bucket: Mapped[TransportMode] = mapped_column(
        enum_type(TransportMode, "revision_transport_mode"), nullable=False
    )
    total_price_eur_cents: Mapped[int | None] = mapped_column(Integer)
    availability: Mapped[Availability] = mapped_column(
        enum_type(Availability, "fare_availability"), nullable=False
    )
    execution_coverage: Mapped[ExecutionCoverage] = mapped_column(
        enum_type(ExecutionCoverage, "execution_coverage"), nullable=False
    )
    source_scope_ratio: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    outbound_departure_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    outbound_arrival_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    return_departure_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    return_arrival_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    outbound_duration_minutes: Mapped[int | None] = mapped_column(Integer)
    return_duration_minutes: Mapped[int | None] = mapped_column(Integer)
    stay_duration_minutes: Mapped[int | None] = mapped_column(Integer)
    transfer_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    itinerary_key: Mapped[str | None] = mapped_column(String(64))

    __table_args__ = (
        UniqueConstraint("fare_snapshot_id", "revision_number", name="uq_snapshot_revision_number"),
        CheckConstraint("revision_number > 0", name="revision_number_positive"),
        CheckConstraint(
            "total_price_eur_cents IS NULL OR total_price_eur_cents > 0",
            name="published_price_positive",
        ),
        CheckConstraint(
            "(availability = 'available' AND total_price_eur_cents IS NOT NULL) "
            "OR (availability <> 'available' AND total_price_eur_cents IS NULL)",
            name="availability_price_consistent",
        ),
        CheckConstraint(
            "source_scope_ratio >= 0 AND source_scope_ratio <= 1", name="source_scope_ratio_range"
        ),
        CheckConstraint(
            "outbound_duration_minutes IS NULL OR outbound_duration_minutes >= 0",
            name="outbound_duration_nonnegative",
        ),
        CheckConstraint(
            "return_duration_minutes IS NULL OR return_duration_minutes >= 0",
            name="return_duration_nonnegative",
        ),
        CheckConstraint(
            "stay_duration_minutes IS NULL OR stay_duration_minutes >= 0",
            name="stay_duration_nonnegative",
        ),
        CheckConstraint("transfer_count >= 0", name="transfer_count_nonnegative"),
        Index("ix_snapshot_revisions_snapshot_price", "fare_snapshot_id", "total_price_eur_cents"),
        Index("ix_snapshot_revisions_crawl_run", "crawl_run_id"),
    )


class FareCellObservation(TimestampMixin, Base):
    """Per-batch cell status, independently retaining the last trusted fare."""

    __tablename__ = "fare_cell_observations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    crawl_run_id: Mapped[int] = mapped_column(
        ForeignKey("crawl_runs.id", ondelete="CASCADE"), nullable=False
    )
    rule_version_id: Mapped[int] = mapped_column(
        ForeignKey("rule_versions.id"), nullable=False
    )
    profile_id: Mapped[int] = mapped_column(
        ForeignKey("search_profiles.id"), nullable=False
    )
    destination_city_id: Mapped[str] = mapped_column(
        ForeignKey("cities.id"), nullable=False
    )
    travel_window_id: Mapped[int] = mapped_column(
        ForeignKey("travel_windows.id"), nullable=False
    )
    mode_bucket: Mapped[TransportMode] = mapped_column(
        enum_type(TransportMode, "observation_transport_mode"), nullable=False
    )
    availability: Mapped[Availability] = mapped_column(
        enum_type(Availability, "observation_availability"), nullable=False
    )
    execution_coverage: Mapped[ExecutionCoverage] = mapped_column(
        enum_type(ExecutionCoverage, "observation_execution_coverage"), nullable=False
    )
    source_scope_ratio: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    trusted_revision_id: Mapped[int | None] = mapped_column(
        ForeignKey("fare_snapshot_revisions.id", ondelete="SET NULL")
    )
    diagnostic: Mapped[str | None] = mapped_column(String(500))

    __table_args__ = (
        UniqueConstraint(
            "crawl_run_id",
            "profile_id",
            "destination_city_id",
            "travel_window_id",
            "mode_bucket",
            name="uq_fare_cell_observations_batch_cell",
        ),
        CheckConstraint(
            "source_scope_ratio >= 0 AND source_scope_ratio <= 1",
            name="observation_source_scope_ratio_range",
        ),
        CheckConstraint(
            "execution_coverage <> 'not_applicable'",
            name="observation_coverage_applicable",
        ),
        CheckConstraint(
            "availability <> 'available' OR trusted_revision_id IS NOT NULL",
            name="available_observation_has_trusted_fare",
        ),
        Index(
            "ix_fare_cell_observations_current_lookup",
            "profile_id",
            "destination_city_id",
            "travel_window_id",
            "mode_bucket",
            "observed_at",
        ),
        Index("ix_fare_cell_observations_crawl_run", "crawl_run_id"),
    )


class SnapshotBooking(Base):
    __tablename__ = "snapshot_bookings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    revision_id: Mapped[int] = mapped_column(
        ForeignKey("fare_snapshot_revisions.id", ondelete="CASCADE"), nullable=False
    )
    component_order: Mapped[int] = mapped_column(Integer, nullable=False)
    source_id: Mapped[str] = mapped_column(ForeignKey("sources.slug"), nullable=False)
    seller: Mapped[str] = mapped_column(String(160), nullable=False)
    fare_name: Mapped[str | None] = mapped_column(String(160))
    pricing_semantics: Mapped[PricingSemantics] = mapped_column(
        enum_type(PricingSemantics, "pricing_semantics"), nullable=False
    )
    original_amount_minor: Mapped[int] = mapped_column(Integer, nullable=False)
    original_currency: Mapped[str] = mapped_column(String(3), nullable=False)
    eur_amount_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    fx_rate: Mapped[float] = mapped_column(Float, default=1.0, nullable=False)
    fx_rate_date: Mapped[date] = mapped_column(Date, nullable=False)
    student_fare_used: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    price_completeness: Mapped[PriceCompleteness] = mapped_column(
        enum_type(PriceCompleteness, "booking_price_completeness"), nullable=False
    )
    mandatory_fees_included: Mapped[bool] = mapped_column(Boolean, nullable=False)
    passenger_age: Mapped[int] = mapped_column(Integer, nullable=False)
    passenger_student: Mapped[bool] = mapped_column(Boolean, nullable=False)
    passenger_eligibility_confirmed: Mapped[bool] = mapped_column(
        Boolean, nullable=False
    )
    bookable_at_observation: Mapped[bool] = mapped_column(Boolean, nullable=False)
    is_teaser: Mapped[bool] = mapped_column(Boolean, nullable=False)
    booking_url: Mapped[str | None] = mapped_column(String(1500))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        UniqueConstraint("revision_id", "component_order", name="uq_snapshot_booking_order"),
        CheckConstraint("component_order >= 0", name="booking_component_order_nonnegative"),
        CheckConstraint("original_amount_minor >= 0", name="booking_original_amount_nonnegative"),
        CheckConstraint("eur_amount_cents >= 0", name="booking_eur_amount_nonnegative"),
        CheckConstraint("fx_rate > 0", name="booking_fx_rate_positive"),
        CheckConstraint(
            "(price_completeness = 'complete' AND mandatory_fees_included = true) "
            "OR (source_id = 'flixbus' AND price_completeness = 'estimated' "
            "AND mandatory_fees_included = false)",
            name="booking_price_policy",
        ),
        CheckConstraint("passenger_age = 22", name="booking_passenger_age_22"),
        CheckConstraint(
            "passenger_eligibility_confirmed = true",
            name="booking_passenger_eligibility_confirmed",
        ),
        CheckConstraint(
            "bookable_at_observation = true",
            name="booking_bookable_at_observation",
        ),
        CheckConstraint("is_teaser = false", name="booking_not_teaser"),
        CheckConstraint(
            "student_fare_used = false OR passenger_student = true",
            name="booking_student_fare_eligible",
        ),
    )


class SnapshotLeg(Base):
    __tablename__ = "snapshot_legs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    revision_id: Mapped[int] = mapped_column(
        ForeignKey("fare_snapshot_revisions.id", ondelete="CASCADE"), nullable=False
    )
    booking_id: Mapped[int | None] = mapped_column(ForeignKey("snapshot_bookings.id"))
    direction: Mapped[Direction] = mapped_column(enum_type(Direction, "leg_direction"), nullable=False)
    segment_order: Mapped[int] = mapped_column(Integer, nullable=False)
    mode: Mapped[TransportMode] = mapped_column(
        enum_type(TransportMode, "leg_transport_mode"), nullable=False
    )
    operator: Mapped[str] = mapped_column(String(160), nullable=False)
    service_number: Mapped[str | None] = mapped_column(String(80))
    origin_terminal_id: Mapped[str] = mapped_column(ForeignKey("terminals.id"), nullable=False)
    destination_terminal_id: Mapped[str] = mapped_column(ForeignKey("terminals.id"), nullable=False)
    departs_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    arrives_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    origin_timezone: Mapped[str] = mapped_column(String(64), nullable=False)
    destination_timezone: Mapped[str] = mapped_column(String(64), nullable=False)
    protection_state: Mapped[ProtectionState] = mapped_column(
        enum_type(ProtectionState, "protection_state"), nullable=False
    )

    __table_args__ = (
        UniqueConstraint(
            "revision_id", "direction", "segment_order", name="uq_snapshot_leg_direction_order"
        ),
        CheckConstraint("arrives_at > departs_at", name="leg_arrival_after_departure"),
        CheckConstraint("segment_order >= 0", name="leg_segment_order_nonnegative"),
    )


class DurationHistogramDaily(Base):
    __tablename__ = "duration_histograms_daily"

    observed_on: Mapped[date] = mapped_column(Date, primary_key=True)
    city_id: Mapped[str] = mapped_column(ForeignKey("cities.id"), primary_key=True)
    mode: Mapped[TransportMode] = mapped_column(
        enum_type(TransportMode, "duration_histogram_mode"), primary_key=True
    )
    is_synthetic: Mapped[bool] = mapped_column(
        Boolean,
        primary_key=True,
        default=False,
        server_default=false(),
    )
    bins: Mapped[dict[str, int]] = mapped_column(JSON_VALUE, default=dict, nullable=False)
    sample_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    __table_args__ = (
        CheckConstraint("sample_count >= 0", name="duration_sample_count_nonnegative"),
    )
