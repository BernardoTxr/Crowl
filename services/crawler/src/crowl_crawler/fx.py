"""Fetch and persist daily ECB euro reference rates."""

from __future__ import annotations

import csv
import io
import math
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from sqlalchemy.orm import Session, sessionmaker

from crowl_core.models import FxRate

from .trust import FX_REFERENCE_CURRENCIES


ECB_REFERENCE_RATES_URL = (
    "https://data-api.ecb.europa.eu/service/data/EXR/"
    "D..EUR.SP00.A?lastNObservations=1&format=csvdata&detail=dataonly"
)
_MAX_RESPONSE_BYTES = 1_000_000
_REQUIRED_COLUMNS = frozenset(
    {"FREQ", "CURRENCY", "CURRENCY_DENOM", "TIME_PERIOD", "OBS_VALUE"}
)


class FxSyncError(RuntimeError):
    """The reference-rate dependency could not be trusted or persisted."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class EcbReferenceRate:
    rate_date: date
    currency: str
    eur_per_unit: float
    published_at: datetime


@dataclass(frozen=True, slots=True)
class FxSyncResult:
    inserted: int
    updated: int
    unchanged: int
    latest_rate_date: date
    currencies: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "status": "succeeded",
            "inserted": self.inserted,
            "updated": self.updated,
            "unchanged": self.unchanged,
            "latest_rate_date": self.latest_rate_date.isoformat(),
            "currencies": list(self.currencies),
        }


Downloader = Callable[[str, float], bytes]


def sync_ecb_reference_rates(
    session_factory: sessionmaker[Session],
    *,
    fetched_at: datetime | None = None,
    timeout_seconds: float = 12.0,
    downloader: Downloader | None = None,
) -> FxSyncResult:
    """Download first, then atomically upsert the trusted daily rates."""

    observed_at = _aware_utc(fetched_at or datetime.now(UTC))
    payload = (downloader or _download)(ECB_REFERENCE_RATES_URL, timeout_seconds)
    rates = parse_ecb_reference_rates(payload, fetched_at=observed_at)

    inserted = 0
    updated = 0
    unchanged = 0
    with session_factory() as session, session.begin():
        for rate in rates:
            existing = session.get(FxRate, (rate.rate_date, rate.currency))
            if existing is None:
                session.add(
                    FxRate(
                        rate_date=rate.rate_date,
                        currency=rate.currency,
                        eur_per_unit=rate.eur_per_unit,
                        published_at=rate.published_at,
                    )
                )
                inserted += 1
            elif math.isclose(
                existing.eur_per_unit,
                rate.eur_per_unit,
                rel_tol=1e-12,
                abs_tol=1e-12,
            ):
                unchanged += 1
            else:
                # A revised value must not look as though it existed before this
                # fetch, so its availability timestamp moves to the new fetch.
                existing.eur_per_unit = rate.eur_per_unit
                existing.published_at = rate.published_at
                updated += 1

    return FxSyncResult(
        inserted=inserted,
        updated=updated,
        unchanged=unchanged,
        latest_rate_date=max(item.rate_date for item in rates),
        currencies=tuple(sorted(item.currency for item in rates)),
    )


def parse_ecb_reference_rates(
    payload: bytes,
    *,
    fetched_at: datetime,
) -> tuple[EcbReferenceRate, ...]:
    """Validate ECB CSV and invert currency-per-EUR quotes into EUR per unit."""

    observed_at = _aware_utc(fetched_at)
    try:
        text = payload.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise FxSyncError("INVALID_ENCODING", "ECB response is not UTF-8 CSV") from error

    reader = csv.DictReader(io.StringIO(text))
    columns = frozenset(reader.fieldnames or ())
    if not _REQUIRED_COLUMNS.issubset(columns):
        raise FxSyncError("SCHEMA_MISMATCH", "ECB CSV is missing required columns")

    parsed: dict[tuple[date, str], EcbReferenceRate] = {}
    for row in reader:
        currency = (row.get("CURRENCY") or "").strip().upper()
        if currency not in FX_REFERENCE_CURRENCIES:
            continue
        if (row.get("FREQ") or "").strip() != "D":
            raise FxSyncError("SCHEMA_MISMATCH", "ECB rate is not daily")
        if (row.get("CURRENCY_DENOM") or "").strip().upper() != "EUR":
            raise FxSyncError("SCHEMA_MISMATCH", "ECB rate is not quoted against EUR")
        try:
            rate_date = date.fromisoformat((row.get("TIME_PERIOD") or "").strip())
            quote_per_euro = Decimal((row.get("OBS_VALUE") or "").strip())
        except (InvalidOperation, ValueError) as error:
            raise FxSyncError("INVALID_VALUE", "ECB rate contains an invalid value") from error
        if rate_date > observed_at.date():
            raise FxSyncError("FUTURE_RATE", "ECB rate date is in the future")
        if not quote_per_euro.is_finite() or quote_per_euro <= 0:
            raise FxSyncError("INVALID_VALUE", "ECB rate must be positive and finite")

        eur_per_unit = float(Decimal(1) / quote_per_euro)
        if not math.isfinite(eur_per_unit) or eur_per_unit <= 0:
            raise FxSyncError("INVALID_VALUE", "inverted ECB rate is invalid")
        key = (rate_date, currency)
        if key in parsed:
            raise FxSyncError("DUPLICATE_RATE", "ECB response contains a duplicate rate")
        parsed[key] = EcbReferenceRate(
            rate_date=rate_date,
            currency=currency,
            eur_per_unit=eur_per_unit,
            published_at=observed_at,
        )

    if not parsed:
        raise FxSyncError("EMPTY_RESPONSE", "ECB response contains no supported rates")
    return tuple(parsed[key] for key in sorted(parsed))


def _download(url: str, timeout_seconds: float) -> bytes:
    if timeout_seconds <= 0:
        raise FxSyncError("INVALID_TIMEOUT", "FX timeout must be positive")
    request = Request(
        url,
        headers={
            "Accept": "text/csv",
            "User-Agent": "Crowl/0.1 (+private fare monitor)",
        },
    )
    try:
        with urlopen(request, timeout=timeout_seconds) as response:  # noqa: S310
            final = urlsplit(response.geturl())
            if final.scheme != "https" or final.hostname != "data-api.ecb.europa.eu":
                raise FxSyncError("UNTRUSTED_REDIRECT", "ECB request left the trusted host")
            payload = response.read(_MAX_RESPONSE_BYTES + 1)
    except FxSyncError:
        raise
    except (HTTPError, URLError, TimeoutError, OSError) as error:
        raise FxSyncError("UPSTREAM_UNAVAILABLE", "ECB rates are unavailable") from error
    if len(payload) > _MAX_RESPONSE_BYTES:
        raise FxSyncError("RESPONSE_TOO_LARGE", "ECB response exceeds the size limit")
    return payload


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise FxSyncError("INVALID_TIMESTAMP", "FX fetch time must be timezone-aware")
    return value.astimezone(UTC)
