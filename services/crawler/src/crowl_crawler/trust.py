"""Fail-closed price, eligibility, FX, and booking-link validation."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from urllib.parse import parse_qsl, urlencode, unquote, urlsplit, urlunsplit

from sqlalchemy import select
from sqlalchemy.orm import Session

from crowl_core.models import FxRate

from .adapters.catalog import load_source_specs
from .models import (
    BookingComponent,
    NormalizedOffer,
    PassengerProfile,
    PriceCompleteness,
)


_CURRENCY_MINOR_UNITS = {
    "BGN": 2,
    "CHF": 2,
    "CZK": 2,
    "DKK": 2,
    "EUR": 2,
    "GBP": 2,
    "HUF": 2,
    "ISK": 0,
    "NOK": 2,
    "PLN": 2,
    "RON": 2,
    "SEK": 2,
    "USD": 2,
}
FX_REFERENCE_CURRENCIES = frozenset(_CURRENCY_MINOR_UNITS) - {"EUR"}
_SENSITIVE_QUERY_KEY = re.compile(
    r"(?:^utm_|^gclid$|^fbclid$|^msclkid$|affiliate|affid|auth|credential|"
    r"email|jwt|key|passenger|phone|secret|session|signature|^sig$|token|"
    r"checkout|cart|basket|^code$|^state$|^nonce$|^pnr$|booking_reference|"
    r"reservation|^ref$|^source$)",
    re.IGNORECASE,
)
_SECRET_PATH = re.compile(
    r"/(?:session|token|auth|checkout|cart|basket)(?:/|=)[A-Za-z0-9._~-]{8,}",
    re.IGNORECASE,
)
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


class OfferRejected(ValueError):
    """An offer is observable, but is not trustworthy enough to publish."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class ConvertedMoney:
    eur_cents: int
    eur_per_unit: float
    rate_date: date


@dataclass(frozen=True, slots=True)
class TrustedBookingComponent:
    component: BookingComponent
    booking_url: str
    eur_amount_cents: int
    fx_rate: float
    fx_rate_date: date


@dataclass(frozen=True, slots=True)
class TrustedOffer:
    offer: NormalizedOffer
    total_eur_cents: int
    bookings: tuple[TrustedBookingComponent, ...]


class FxRateConverter:
    """Convert persisted provider money using a prior published reference rate."""

    def __init__(self, *, maximum_age: timedelta = timedelta(days=7)) -> None:
        if maximum_age <= timedelta(0):
            raise ValueError("FX maximum age must be positive")
        self.maximum_age = maximum_age

    def convert(
        self,
        session: Session,
        *,
        amount_minor: int,
        currency: str,
        observed_at: datetime,
    ) -> ConvertedMoney:
        if amount_minor <= 0:
            raise OfferRejected("invalid_amount", "money amount must be positive")
        if observed_at.tzinfo is None or observed_at.utcoffset() is None:
            raise OfferRejected("invalid_observation_time", "FX conversion requires an aware time")
        currency = currency.upper()
        minor_units = _CURRENCY_MINOR_UNITS.get(currency)
        if minor_units is None:
            raise OfferRejected("unsupported_currency", f"unsupported currency: {currency}")

        observed_utc = observed_at.astimezone(UTC)
        observed_date = observed_utc.date()
        if currency == "EUR":
            return ConvertedMoney(
                eur_cents=amount_minor,
                eur_per_unit=1.0,
                rate_date=observed_date,
            )

        rate = session.scalar(
            select(FxRate)
            .where(
                FxRate.currency == currency,
                FxRate.rate_date <= observed_date,
                FxRate.published_at <= observed_utc,
            )
            .order_by(FxRate.rate_date.desc(), FxRate.published_at.desc())
            .limit(1)
        )
        if rate is None:
            raise OfferRejected(
                "missing_fx_rate",
                f"no published EUR conversion rate is available for {currency}",
            )
        if observed_date - rate.rate_date > self.maximum_age:
            raise OfferRejected(
                "stale_fx_rate",
                f"the persisted {currency} conversion rate is too old",
            )
        if not math.isfinite(rate.eur_per_unit) or rate.eur_per_unit <= 0:
            raise OfferRejected("invalid_fx_rate", "persisted FX rate is invalid")

        original_units = Decimal(amount_minor) / (Decimal(10) ** minor_units)
        eur_cents = int(
            (original_units * Decimal(str(rate.eur_per_unit)) * Decimal(100)).quantize(
                Decimal("1"), rounding=ROUND_HALF_UP
            )
        )
        if eur_cents <= 0:
            raise OfferRejected("invalid_converted_amount", "converted EUR amount is not positive")
        return ConvertedMoney(
            eur_cents=eur_cents,
            eur_per_unit=rate.eur_per_unit,
            rate_date=rate.rate_date,
        )


class OfferTrustPolicy:
    """Validate that a normalized offer may compete as an advertised fare."""

    def __init__(
        self,
        booking_hosts: dict[str, tuple[str, ...]],
        *,
        fx_converter: FxRateConverter | None = None,
    ) -> None:
        self.booking_hosts = {
            source: tuple(_canonical_host(host) for host in hosts)
            for source, hosts in booking_hosts.items()
        }
        self.fx_converter = fx_converter or FxRateConverter()

    @classmethod
    def from_source_catalog(cls) -> OfferTrustPolicy:
        hosts = {spec.slug: spec.booking_hosts for spec in load_source_specs()}
        hosts["fixture"] = ("example.invalid",)
        return cls(hosts)

    def validate(
        self,
        session: Session,
        *,
        offer: NormalizedOffer,
        passenger: PassengerProfile,
    ) -> TrustedOffer:
        if offer.price_completeness is not PriceCompleteness.COMPLETE:
            raise OfferRejected("incomplete_price", "only complete prices may be published")
        if not offer.mandatory_fees_included:
            raise OfferRejected("missing_mandatory_fees", "mandatory fees are not complete")
        if not offer.passenger_eligibility_confirmed:
            raise OfferRejected("unconfirmed_eligibility", "passenger eligibility is unconfirmed")
        if (
            offer.passenger_age != passenger.age
            or offer.passenger_student != passenger.student
        ):
            raise OfferRejected("wrong_passenger", "offer does not match the search passenger")
        if not offer.bookable or offer.is_teaser:
            raise OfferRejected("not_bookable", "teaser or non-bookable fares are excluded")
        if offer.expires_at is not None and offer.expires_at <= offer.observed_at:
            raise OfferRejected("expired_offer", "offer was already expired when observed")
        if not offer.booking_components:
            raise OfferRejected("missing_booking", "a published offer needs a booking component")

        total = self.fx_converter.convert(
            session,
            amount_minor=offer.total_amount_minor,
            currency=offer.currency,
            observed_at=offer.observed_at,
        )
        bookings: list[TrustedBookingComponent] = []
        for component in offer.booking_components:
            self._validate_component(component, passenger)
            hosts = self.booking_hosts.get(component.source_slug)
            if not hosts:
                raise OfferRejected(
                    "missing_booking_host_policy",
                    f"no booking host policy exists for {component.source_slug}",
                )
            booking_url = sanitize_booking_url(component.booking_url, hosts)
            converted = self.fx_converter.convert(
                session,
                amount_minor=component.amount_minor,
                currency=component.currency,
                observed_at=offer.observed_at,
            )
            bookings.append(
                TrustedBookingComponent(
                    component=component,
                    booking_url=booking_url,
                    eur_amount_cents=converted.eur_cents,
                    fx_rate=converted.eur_per_unit,
                    fx_rate_date=converted.rate_date,
                )
            )

        booking_total = sum(item.eur_amount_cents for item in bookings)
        rounding_tolerance = max(1, len(bookings))
        if abs(booking_total - total.eur_cents) > rounding_tolerance:
            raise OfferRejected(
                "component_total_mismatch",
                "independently bookable components do not match the advertised total",
            )
        return TrustedOffer(
            offer=offer,
            total_eur_cents=booking_total,
            bookings=tuple(bookings),
        )

    @staticmethod
    def _validate_component(
        component: BookingComponent,
        passenger: PassengerProfile,
    ) -> None:
        if not component.mandatory_fees_included:
            raise OfferRejected(
                "missing_component_fees",
                "a booking component does not include mandatory fees",
            )
        if not component.passenger_eligibility_confirmed:
            raise OfferRejected(
                "unconfirmed_component_eligibility",
                "a booking component has unconfirmed passenger eligibility",
            )
        if (
            component.passenger_age != passenger.age
            or component.passenger_student != passenger.student
        ):
            raise OfferRejected(
                "wrong_component_passenger",
                "a booking component does not match the search passenger",
            )
        if component.student_fare_applied and not passenger.student:
            raise OfferRejected(
                "student_fare_ineligible",
                "a student fare was applied to a non-student passenger",
            )
        if not component.bookable or component.is_teaser:
            raise OfferRejected(
                "component_not_bookable",
                "a booking component is a teaser or is not independently bookable",
            )


def sanitize_booking_url(url: str, approved_hosts: tuple[str, ...]) -> str:
    """Return a canonical, non-secret HTTPS booking URL or reject it."""

    if not url or len(url) > 1500 or _CONTROL.search(url) or "\\" in url:
        raise OfferRejected("unsafe_booking_url", "booking URL is malformed")
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError as error:
        raise OfferRejected("unsafe_booking_url", "booking URL is malformed") from error
    if parsed.scheme.lower() != "https" or not parsed.hostname:
        raise OfferRejected("unsafe_booking_url", "booking URL must use HTTPS")
    if parsed.username is not None or parsed.password is not None:
        raise OfferRejected("unsafe_booking_url", "booking URL may not contain credentials")
    if port not in {None, 443}:
        raise OfferRejected("unsafe_booking_url", "booking URL uses a non-standard port")

    host = _canonical_host(parsed.hostname)
    allowed = tuple(_canonical_host(item) for item in approved_hosts)
    if not any(host == item or host.endswith(f".{item}") for item in allowed):
        raise OfferRejected("unapproved_booking_host", "booking URL host is not approved")

    decoded_path = unquote(parsed.path)
    if (
        _CONTROL.search(decoded_path)
        or "\\" in decoded_path
        or _SECRET_PATH.search(decoded_path)
    ):
        raise OfferRejected(
            "secret_booking_path",
            "booking URL path appears to contain a transient secret",
        )
    try:
        query_items = parse_qsl(
            parsed.query,
            keep_blank_values=True,
            strict_parsing=False,
            max_num_fields=64,
        )
    except ValueError as error:
        raise OfferRejected("unsafe_booking_url", "booking URL query is malformed") from error
    safe_query = [
        (key, value)
        for key, value in query_items
        if not _SENSITIVE_QUERY_KEY.search(key)
        and not _CONTROL.search(key)
        and not _CONTROL.search(value)
    ]
    netloc = host if port is None else f"{host}:443"
    return urlunsplit(
        ("https", netloc, parsed.path or "/", urlencode(safe_query, doseq=True), "")
    )


def _canonical_host(host: str) -> str:
    normalized = host.rstrip(".").lower()
    try:
        return normalized.encode("idna").decode("ascii")
    except UnicodeError as error:
        raise OfferRejected("unsafe_booking_host", "booking host is invalid") from error
