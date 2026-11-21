from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import StrEnum
from typing import Any, Mapping


class TransportMode(StrEnum):
    FLIGHT = "flight"
    TRAIN = "train"
    BUS = "bus"
    MIXED = "mixed"


class ExtractionStrategy(StrEnum):
    HTTP_STRUCTURED = "http_structured"
    HTTP_HTML = "http_html"
    PLAYWRIGHT = "playwright"
    DISABLED = "disabled"
    FIXTURE = "fixture"


class BatchStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    PARTIAL = "partial"
    FAILED = "failed"
    ABANDONED = "abandoned"


class ExtractorRunStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    PARTIAL = "partial"
    FAILED = "failed"
    BLOCKED = "blocked"
    QUARANTINED = "quarantined"
    DISABLED = "disabled"


class SearchOutcome(StrEnum):
    SUCCESS_RESULTS = "success_results"
    SUCCESS_EMPTY = "success_empty"
    NOT_ON_SALE = "not_on_sale"
    NO_SERVICE = "no_service"
    SOLD_OUT = "sold_out"
    PARTIAL_SUCCESS = "partial_success"
    BLOCKED = "blocked"
    CAPTCHA_CHALLENGE = "captcha_challenge"
    RATE_LIMITED = "rate_limited"
    TIMEOUT = "timeout"
    UPSTREAM_ERROR = "upstream_error"
    PARSE_SCHEMA_ERROR = "parse_schema_error"
    CONFIGURATION_ERROR = "configuration_error"
    CANCELLED_DEADLINE = "cancelled_deadline"
    SKIPPED_DISABLED = "skipped_disabled"
    SKIPPED_UNSUPPORTED_ROUTE = "skipped_unsupported_route"


class SearchKind(StrEnum):
    ONE_WAY = "one_way"
    ROUND_TRIP = "round_trip"


class Direction(StrEnum):
    OUTBOUND = "outbound"
    RETURN = "return"


class PricingSemantics(StrEnum):
    ONE_WAY = "one_way"
    ROUND_TRIP_ONLY = "round_trip_only"
    COMPOSABLE = "composable"


class PriceCompleteness(StrEnum):
    COMPLETE = "complete"
    ESTIMATED = "estimated"
    UNKNOWN = "unknown"


SUCCESSFUL_SEARCH_OUTCOMES = frozenset(
    {
        SearchOutcome.SUCCESS_RESULTS,
        SearchOutcome.SUCCESS_EMPTY,
        SearchOutcome.NOT_ON_SALE,
        SearchOutcome.NO_SERVICE,
        SearchOutcome.SOLD_OUT,
        SearchOutcome.SKIPPED_UNSUPPORTED_ROUTE,
    }
)

BLOCKING_SEARCH_OUTCOMES = frozenset(
    {SearchOutcome.BLOCKED, SearchOutcome.CAPTCHA_CHALLENGE}
)

RETRYABLE_SEARCH_OUTCOMES = frozenset(
    {
        SearchOutcome.RATE_LIMITED,
        SearchOutcome.TIMEOUT,
        SearchOutcome.UPSTREAM_ERROR,
    }
)

_SAFE_DIAGNOSTIC = re.compile(r"^[A-Z0-9][A-Z0-9_.:-]{0,63}$")


def require_aware(value: datetime, field_name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")


def validate_diagnostic_code(value: str) -> str:
    """Allow only short, non-secret machine codes in monitoring records."""

    if not _SAFE_DIAGNOSTIC.fullmatch(value):
        raise ValueError(
            "diagnostic_code must contain only uppercase letters, digits, '.', ':', '_' or '-'"
        )
    return value


@dataclass(frozen=True, slots=True)
class TimeWindow:
    starts_at: datetime
    ends_at: datetime
    timezone: str

    def __post_init__(self) -> None:
        require_aware(self.starts_at, "starts_at")
        require_aware(self.ends_at, "ends_at")
        if self.ends_at <= self.starts_at:
            raise ValueError("time window must have positive duration")
        if not self.timezone:
            raise ValueError("timezone is required")

    def as_fingerprint_data(self) -> Mapping[str, str]:
        return {
            "starts_at": self.starts_at.isoformat(),
            "ends_at": self.ends_at.isoformat(),
            "timezone": self.timezone,
        }


@dataclass(frozen=True, slots=True)
class PassengerProfile:
    adults: int = 1
    age: int = 22
    student: bool = True
    paid_entitlements: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.adults != 1:
            raise ValueError("Crowl v1 supports exactly one adult")
        if not 0 <= self.age <= 120:
            raise ValueError("passenger age is out of range")


@dataclass(frozen=True, slots=True)
class SearchRequest:
    batch_id: str
    query_id: str
    destination_city_id: str
    weekend_start: date
    search_kind: SearchKind
    outbound_window: TimeWindow
    return_window: TimeWindow | None
    origin_terminal_ids: tuple[str, ...]
    destination_terminal_ids: tuple[str, ...]
    passenger: PassengerProfile = field(default_factory=PassengerProfile)
    direction: Direction | None = None
    currency: str = "EUR"
    baggage: str = "smallest_included_personal_item"
    maximum_direction_minutes: int = 720

    def __post_init__(self) -> None:
        if not self.batch_id or not self.query_id or not self.destination_city_id:
            raise ValueError("batch_id, query_id and destination_city_id are required")
        if not self.origin_terminal_ids or not self.destination_terminal_ids:
            raise ValueError("origin and destination terminal sets may not be empty")
        if self.search_kind is SearchKind.ROUND_TRIP and self.return_window is None:
            raise ValueError("round-trip requests require a return window")
        if self.search_kind is SearchKind.ONE_WAY and self.direction is None:
            raise ValueError("one-way requests require a direction")
        if len(self.currency) != 3 or self.currency.upper() != self.currency:
            raise ValueError("currency must be a three-letter uppercase code")
        if self.maximum_direction_minutes <= 0:
            raise ValueError("maximum direction duration must be positive")

    @property
    def fingerprint(self) -> str:
        payload: dict[str, Any] = {
            "destination_city_id": self.destination_city_id,
            "weekend_start": self.weekend_start.isoformat(),
            "search_kind": self.search_kind.value,
            "direction": self.direction.value if self.direction else None,
            "outbound_window": self.outbound_window.as_fingerprint_data(),
            "return_window": (
                self.return_window.as_fingerprint_data() if self.return_window else None
            ),
            "origin_terminal_ids": sorted(self.origin_terminal_ids),
            "destination_terminal_ids": sorted(self.destination_terminal_ids),
            "passenger": {
                "adults": self.passenger.adults,
                "age": self.passenger.age,
                "student": self.passenger.student,
                "paid_entitlements": sorted(self.passenger.paid_entitlements),
            },
            "currency": self.currency,
            "baggage": self.baggage,
            "maximum_direction_minutes": self.maximum_direction_minutes,
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class Segment:
    sequence: int
    mode: TransportMode
    operator: str
    service_number: str | None
    departure_terminal_id: str
    departure_terminal_name: str
    arrival_terminal_id: str
    arrival_terminal_name: str
    departs_at: datetime
    arrives_at: datetime
    protected_connection: bool = True

    def __post_init__(self) -> None:
        require_aware(self.departs_at, "departs_at")
        require_aware(self.arrives_at, "arrives_at")
        if self.sequence < 1:
            raise ValueError("segment sequence starts at one")
        if self.arrives_at <= self.departs_at:
            raise ValueError("segment arrival must be after departure")
        required = (
            self.operator,
            self.departure_terminal_id,
            self.departure_terminal_name,
            self.arrival_terminal_id,
            self.arrival_terminal_name,
        )
        if not all(required):
            raise ValueError("segment operator and exact terminals are required")


@dataclass(frozen=True, slots=True)
class BookingComponent:
    seller: str
    source_slug: str
    amount_minor: int
    currency: str
    booking_url: str | None
    fare_name: str
    mandatory_fees_included: bool
    passenger_age: int
    passenger_student: bool
    passenger_eligibility_confirmed: bool
    bookable: bool
    is_teaser: bool
    student_fare_applied: bool = False
    price_completeness: PriceCompleteness = PriceCompleteness.COMPLETE

    def __post_init__(self) -> None:
        if self.amount_minor <= 0:
            raise ValueError("booking component amount must be positive")
        if self.currency != self.currency.upper() or len(self.currency) != 3:
            raise ValueError("booking component currency is invalid")
        if self.booking_url is not None and not self.booking_url.startswith("https://"):
            raise ValueError("booking URL must use HTTPS")
        if not 0 <= self.passenger_age <= 120:
            raise ValueError("booking component passenger age is invalid")
        if self.student_fare_applied and not self.passenger_student:
            raise ValueError("a student fare requires a student passenger profile")
        trust_flags = (
            self.mandatory_fees_included,
            self.passenger_student,
            self.passenger_eligibility_confirmed,
            self.bookable,
            self.is_teaser,
            self.student_fare_applied,
        )
        if any(not isinstance(value, bool) for value in trust_flags):
            raise ValueError("booking component trust flags must be booleans")


@dataclass(frozen=True, slots=True)
class NormalizedOffer:
    provider_offer_id: str
    source_slug: str
    destination_city_id: str
    pricing_semantics: PricingSemantics
    price_completeness: PriceCompleteness
    total_amount_minor: int
    currency: str
    mandatory_fees_included: bool
    passenger_age: int
    passenger_student: bool
    passenger_eligibility_confirmed: bool
    bookable: bool
    is_teaser: bool
    observed_at: datetime
    outbound_segments: tuple[Segment, ...]
    return_segments: tuple[Segment, ...] = ()
    booking_components: tuple[BookingComponent, ...] = ()
    included_baggage: str = "smallest_included_personal_item"
    included_transfer_cost_eur_cents: int = 0
    expires_at: datetime | None = None

    def __post_init__(self) -> None:
        require_aware(self.observed_at, "observed_at")
        if self.expires_at is not None:
            require_aware(self.expires_at, "expires_at")
        if self.total_amount_minor <= 0:
            raise ValueError("offer total must be positive")
        if self.currency != self.currency.upper() or len(self.currency) != 3:
            raise ValueError("offer currency is invalid")
        if not 0 <= self.passenger_age <= 120:
            raise ValueError("offer passenger age is invalid")
        if self.included_transfer_cost_eur_cents < 0:
            raise ValueError("included transfer cost cannot be negative")
        trust_flags = (
            self.mandatory_fees_included,
            self.passenger_student,
            self.passenger_eligibility_confirmed,
            self.bookable,
            self.is_teaser,
        )
        if any(not isinstance(value, bool) for value in trust_flags):
            raise ValueError("offer trust flags must be booleans")
        if not self.outbound_segments:
            raise ValueError("an offer needs at least one outbound segment")
        if (
            self.pricing_semantics is PricingSemantics.ROUND_TRIP_ONLY
            and not self.return_segments
        ):
            raise ValueError("atomic round-trip offers need return segments")
        if self.booking_components:
            component_total = sum(
                component.amount_minor for component in self.booking_components
            )
            same_currency = all(
                component.currency == self.currency
                for component in self.booking_components
            )
            if same_currency and component_total != self.total_amount_minor:
                raise ValueError(
                    "same-currency booking components must sum to the offer total"
                )


@dataclass(frozen=True, slots=True)
class ConnectorResult:
    outcome: SearchOutcome
    retryable: bool
    diagnostic_code: str
    expected_count: int
    completed_count: int
    offers: tuple[NormalizedOffer, ...]
    parser_version: str
    observed_at: datetime
    response_fingerprint: str | None = None
    schema_fingerprint: str | None = None

    def __post_init__(self) -> None:
        require_aware(self.observed_at, "observed_at")
        validate_diagnostic_code(self.diagnostic_code)
        if self.expected_count < 0 or self.completed_count < 0:
            raise ValueError("result counts may not be negative")
        if self.completed_count > self.expected_count:
            raise ValueError("completed count may not exceed expected count")
        if self.outcome is SearchOutcome.SUCCESS_RESULTS and not self.offers:
            raise ValueError("success_results must contain at least one offer")
        if self.outcome is not SearchOutcome.SUCCESS_RESULTS and self.offers:
            if self.outcome is not SearchOutcome.PARTIAL_SUCCESS:
                raise ValueError(
                    "only result or partial-success outcomes may contain offers"
                )
        if self.retryable and self.outcome not in RETRYABLE_SEARCH_OUTCOMES:
            raise ValueError("only transient outcomes may be retryable")

    @classmethod
    def failure(
        cls,
        *,
        outcome: SearchOutcome,
        diagnostic_code: str,
        parser_version: str,
        observed_at: datetime,
        retryable: bool | None = None,
    ) -> "ConnectorResult":
        if (
            outcome in SUCCESSFUL_SEARCH_OUTCOMES
            or outcome is SearchOutcome.PARTIAL_SUCCESS
        ):
            raise ValueError("failure() requires a failure outcome")
        return cls(
            outcome=outcome,
            retryable=(
                outcome in RETRYABLE_SEARCH_OUTCOMES if retryable is None else retryable
            ),
            diagnostic_code=diagnostic_code,
            expected_count=1,
            completed_count=0,
            offers=(),
            parser_version=parser_version,
            observed_at=observed_at,
        )


@dataclass(frozen=True, slots=True)
class AdapterMetadata:
    source_slug: str
    display_name: str
    modes: tuple[TransportMode, ...]
    strategy: ExtractionStrategy
    intended_strategy: ExtractionStrategy
    connector_version: str
    parser_version: str
    enabled: bool
    disabled_reason: str | None
    access_basis: str
    markets: tuple[str, ...] = ("schengen",)
    sale_horizon_days: int | None = None

    def __post_init__(self) -> None:
        if not self.source_slug or not self.display_name or not self.modes:
            raise ValueError("source slug, display name, and modes are required")
        if self.enabled and self.strategy is ExtractionStrategy.DISABLED:
            raise ValueError("an enabled adapter cannot use the disabled strategy")
        if not self.enabled and not self.disabled_reason:
            raise ValueError("a disabled adapter must explain why")
        if not self.enabled and self.strategy is not ExtractionStrategy.DISABLED:
            raise ValueError("disabled adapters must report the disabled strategy")
        if self.enabled and self.disabled_reason is not None:
            raise ValueError("enabled adapters may not carry a disabled reason")


@dataclass(frozen=True, slots=True)
class CoverageDiscovery:
    source_slug: str
    supported_terminal_pairs: tuple[tuple[str, str], ...]
    sellable_through: date | None
    observed_at: datetime
    diagnostic_code: str

    def __post_init__(self) -> None:
        require_aware(self.observed_at, "observed_at")
        validate_diagnostic_code(self.diagnostic_code)
