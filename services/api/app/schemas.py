from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from crowl_core.enums import (
    Availability,
    BatchStatus,
    ExecutionCoverage,
    ExtractorRunStatus,
    ModeFilter,
    PriceCompleteness,
    TransportMode,
)


class APIModel(BaseModel):
    model_config = ConfigDict(use_enum_values=True, populate_by_name=True)


class Coordinates(APIModel):
    lat: float
    lon: float


class Country(APIModel):
    code: str
    name: str


class CityStatus(APIModel):
    visited: bool
    wishlist: bool


class Photo(APIModel):
    asset: str
    alt: str
    creator: str | None = None
    source_url: str | None = None
    license: str | None = None
    attribution: str | None = None
    curation_status: str
    object_position: str


class CitySummary(APIModel):
    id: str
    slug: str
    name: str
    country: Country
    timezone: str
    coordinates: Coordinates
    status: CityStatus
    categories: list[str]
    attributes: dict[str, int]
    summary: str
    photo: Photo | None = None


class CitiesResponse(APIModel):
    items: list[CitySummary]
    total: int


class Definition(APIModel):
    id: str
    label: str
    min: int | None = None
    max: int | None = None
    low_label: str | None = None
    high_label: str | None = None


class OriginMeta(APIModel):
    id: str
    name: str
    timezone: str
    coordinates: Coordinates


class MetaResponse(APIModel):
    api_version: Literal["v1"] = "v1"
    owner_mode: Literal["private_single_user"] = "private_single_user"
    currency: Literal["EUR"] = "EUR"
    catalog_variant: Literal["final"]
    origin: OriginMeta
    passenger: dict[str, Any]
    rules: dict[str, Any]
    modes: list[str]
    category_definitions: list[dict[str, Any]]
    attribute_definitions: list[dict[str, Any]]


class Weekend(APIModel):
    friday: date
    starts_at: datetime
    ends_at: datetime
    label: str
    minimum_stay_hours: int
    max_direction_hours: int


class WeekendsResponse(APIModel):
    items: list[Weekend]


class TerminalBrief(APIModel):
    id: str
    name: str
    kind: str
    timezone: str


class MapPriceItem(APIModel):
    city: CitySummary
    price_eur_cents: int | None
    winning_mode: TransportMode | None
    rule_version_id: int | None
    profile_id: int | None
    availability: Availability
    execution_coverage: ExecutionCoverage
    source_scope_ratio: float
    is_synthetic: bool
    freshness: Literal["fresh", "stale", "never_observed"]
    # `observed_at` belongs to the retained fare. The current fields describe
    # the latest batch independently, including failed/empty observations.
    observed_at: datetime | None
    current_observed_at: datetime | None
    current_crawl_run_id: int | None
    current_diagnostic: str | None
    origin_terminal: TerminalBrief | None
    destination_terminal: TerminalBrief | None
    return_origin_terminal: TerminalBrief | None
    return_destination_terminal: TerminalBrief | None
    revision_id: int | None
    price_completeness: PriceCompleteness | None
    mandatory_fees_included: bool | None
    fare_disclosure: str | None


class MapPricesResponse(APIModel):
    weekend: date
    mode: ModeFilter
    observed_at: datetime | None
    price_scale_breakpoints_eur_cents: list[int]
    items: list[MapPriceItem]


class TypicalDuration(APIModel):
    mode: TransportMode
    state: Literal["available", "not_enough_data"]
    p25_minutes: int | None = None
    median_minutes: int | None = None
    p75_minutes: int | None = None
    sample_count: int
    is_synthetic: bool


class CityDetailResponse(APIModel):
    city: CitySummary
    typical_durations: list[TypicalDuration]


class PriceHistoryCell(APIModel):
    weekend: date
    observed_on: date
    lead_days: int | None
    price_eur_cents: int | None
    availability: Availability
    execution_coverage: ExecutionCoverage
    source_scope_ratio: float
    is_synthetic: bool
    freshness: Literal["fresh", "stale", "never_observed"]
    observed_at: datetime | None
    current_observed_at: datetime
    current_crawl_run_id: int
    current_diagnostic: str | None
    revision_id: int | None
    rule_version_id: int
    profile_id: int
    winning_mode: TransportMode | None
    price_completeness: PriceCompleteness | None
    mandatory_fees_included: bool | None
    fare_disclosure: str | None


class PriceScale(APIModel):
    p10_eur_cents: int | None
    p90_eur_cents: int | None


class PriceHistoryResponse(APIModel):
    city: CitySummary
    mode: ModeFilter
    scale: PriceScale
    cells: list[PriceHistoryCell]


class Booking(APIModel):
    id: int
    component_order: int
    source: str
    seller: str
    fare_name: str | None
    pricing_semantics: str
    original_amount_minor: int
    original_currency: str
    eur_amount_cents: int
    fx_rate: float
    fx_rate_date: date
    student_fare_used: bool
    price_completeness: PriceCompleteness
    mandatory_fees_included: bool
    passenger_age: int
    passenger_student: bool
    passenger_eligibility_confirmed: bool
    bookable_at_observation: bool
    is_teaser: bool
    booking_url: str | None
    expires_at: datetime | None
    fare_disclosure: str | None


class Leg(APIModel):
    id: int
    booking_id: int | None
    direction: str
    segment_order: int
    mode: TransportMode
    operator: str
    service_number: str | None
    origin_terminal: TerminalBrief
    destination_terminal: TerminalBrief
    departs_at: datetime
    arrives_at: datetime
    origin_timezone: str
    destination_timezone: str
    protection_state: str


class Journey(APIModel):
    revision_id: int
    rule_version_id: int
    profile_id: int
    total_price_eur_cents: int
    mode: TransportMode
    observed_at: datetime
    published_at: datetime
    historical: bool
    availability: Availability
    execution_coverage: ExecutionCoverage
    source_scope_ratio: float
    is_synthetic: bool
    freshness: Literal["fresh", "stale"]
    outbound_departure_at: datetime | None
    outbound_arrival_at: datetime | None
    return_departure_at: datetime | None
    return_arrival_at: datetime | None
    outbound_duration_minutes: int | None
    return_duration_minutes: int | None
    stay_duration_minutes: int | None
    transfer_count: int
    price_completeness: PriceCompleteness
    mandatory_fees_included: bool
    fare_disclosure: str | None
    bookings: list[Booking]
    legs: list[Leg]


class JourneysResponse(APIModel):
    city: CitySummary
    weekend: date
    mode: ModeFilter
    availability: Availability
    execution_coverage: ExecutionCoverage
    source_scope_ratio: float
    freshness: Literal["fresh", "stale", "never_observed"]
    # Retained-fare observation time; current batch state is separate below.
    observed_at: datetime | None
    current_observed_at: datetime | None
    current_crawl_run_id: int | None
    current_diagnostic: str | None
    items: list[Journey]


class Batch(APIModel):
    id: int
    scheduled_for: date
    status: BatchStatus
    started_at: datetime | None
    heartbeat_at: datetime | None
    completed_at: datetime | None
    published_at: datetime | None
    expected_cells: int
    completed_cells: int
    successful_cells: int
    requested_sources: int
    enabled_sources: int
    execution_coverage_ratio: float
    latest_trustworthy_observation_at: datetime | None
    diagnostic: str | None


class LatestBatchResponse(APIModel):
    batch: Batch | None
    state: Literal["not_started", "current", "stale"]
    next_expected_batch_at: datetime


class ExtractorCounts(APIModel):
    expected: int
    attempted: int
    successful: int
    empty: int
    result_bearing: int
    offers: int
    physical_requests: int


class Extractor(APIModel):
    source: str
    display_name: str
    modes: list[str]
    strategy: str
    connector_version: str
    parser_version: str
    enabled: bool
    disabled_reason: str | None
    state: ExtractorRunStatus
    last_attempt_at: datetime | None
    last_success_at: datetime | None
    freshness: Literal["fresh", "stale", "never_observed"]
    runtime_ms: int | None
    consecutive_failures: int
    execution_coverage_ratio: float
    source_scope_ratio: float
    counts: ExtractorCounts
    diagnostic: str | None


class ExtractorsResponse(APIModel):
    batch_id: int | None
    requested_sources: int
    enabled_sources: int
    items: list[Extractor]


class ExtractorRunItem(APIModel):
    id: int
    batch_id: int
    scheduled_for: date
    state: ExtractorRunStatus
    started_at: datetime | None
    completed_at: datetime | None
    last_success_at: datetime | None
    runtime_ms: int | None
    consecutive_failures: int
    counts: ExtractorCounts
    diagnostic: str | None


class ExtractorRunsResponse(APIModel):
    source: str
    items: list[ExtractorRunItem]


class HealthResponse(APIModel):
    status: Literal["ok", "not_ready"]
    service: Literal["crowl-api"] = "crowl-api"
    database: Literal["reachable", "unreachable", "not_checked"]
    schema_revision: str | None = Field(default=None, alias="schema", serialization_alias="schema")
    detail: str | None = Field(default=None, max_length=300)
