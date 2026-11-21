"""Canonical string enums shared by storage, API and crawler code."""

from enum import StrEnum


FLIXBUS_DISPLAYED_FARE_DISCLOSURE = "Displayed fare — service fees may apply"


class TransportMode(StrEnum):
    FLIGHT = "flight"
    TRAIN = "train"
    BUS = "bus"
    MIXED = "mixed"


class ModeFilter(StrEnum):
    ALL = "all"
    FLIGHT = "flight"
    TRAIN = "train"
    BUS = "bus"
    MIXED = "mixed"


class TerminalKind(StrEnum):
    AIRPORT = "airport"
    RAIL = "rail"
    BUS = "bus"


class LinkRelationship(StrEnum):
    PRIMARY = "primary"
    PRIMARY_AIRPORT = "primary_airport"
    PRIMARY_STATION = "primary_station"
    PRIMARY_BUS = "primary_bus"
    NEARBY_AIRPORT = "nearby_airport"
    NEARBY_STATION = "nearby_station"
    NEARBY_BUS = "nearby_bus"


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


class Availability(StrEnum):
    AVAILABLE = "available"
    NO_VALID_OPTION = "no_valid_option"
    NOT_ON_SALE = "not_on_sale"
    SOLD_OUT = "sold_out"
    UNKNOWN_DUE_FAILURE = "unknown_due_failure"


class ExecutionCoverage(StrEnum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    FAILED = "failed"
    NOT_APPLICABLE = "not_applicable"


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


class PriceCompleteness(StrEnum):
    COMPLETE = "complete"
    ESTIMATED = "estimated"
    UNKNOWN = "unknown"


class PricingSemantics(StrEnum):
    ONE_WAY = "one_way"
    ROUND_TRIP_ONLY = "round_trip_only"
    COMPOSABLE = "composable"


class Direction(StrEnum):
    OUTBOUND = "outbound"
    RETURN = "return"


class ProtectionState(StrEnum):
    PROTECTED = "protected"
    UNPROTECTED_TRANSFER = "unprotected_transfer"
    NOT_APPLICABLE = "not_applicable"
