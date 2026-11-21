from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

from crowl_core import Base, create_database_engine, create_session_factory
from crowl_core.models import FxRate
from crowl_crawler.models import (
    BookingComponent,
    NormalizedOffer,
    PassengerProfile,
    PriceCompleteness,
    PricingSemantics,
    Segment,
    TransportMode,
)
from crowl_crawler.trust import (
    FxRateConverter,
    OfferRejected,
    OfferTrustPolicy,
    sanitize_booking_url,
)


OBSERVED_AT = datetime(2027, 1, 1, 2, tzinfo=UTC)


def _offer() -> NormalizedOffer:
    component = BookingComponent(
        seller="Transavia",
        source_slug="transavia",
        amount_minor=10_000,
        currency="EUR",
        booking_url=(
            "https://booking.transavia.com/search?from=ORY&"
            "utm_source=crowl&sessionToken=top-secret#checkout"
        ),
        fare_name="Basic",
        mandatory_fees_included=True,
        passenger_age=22,
        passenger_student=True,
        passenger_eligibility_confirmed=True,
        bookable=True,
        is_teaser=False,
        student_fare_applied=False,
    )
    outbound = Segment(
        sequence=1,
        mode=TransportMode.FLIGHT,
        operator="Transavia",
        service_number="TO100",
        departure_terminal_id="par-ory",
        departure_terminal_name="Paris Orly",
        arrival_terminal_id="lis-humberto-delgado",
        arrival_terminal_name="Lisbon Airport",
        departs_at=datetime(2027, 1, 8, 20, tzinfo=UTC),
        arrives_at=datetime(2027, 1, 8, 22, tzinfo=UTC),
    )
    inbound = replace(
        outbound,
        departure_terminal_id="lis-humberto-delgado",
        departure_terminal_name="Lisbon Airport",
        arrival_terminal_id="par-ory",
        arrival_terminal_name="Paris Orly",
        departs_at=datetime(2027, 1, 10, 19, tzinfo=UTC),
        arrives_at=datetime(2027, 1, 10, 21, tzinfo=UTC),
    )
    return NormalizedOffer(
        provider_offer_id="transavia-test",
        source_slug="transavia",
        destination_city_id="lisbon-pt",
        pricing_semantics=PricingSemantics.ROUND_TRIP_ONLY,
        price_completeness=PriceCompleteness.COMPLETE,
        total_amount_minor=10_000,
        currency="EUR",
        mandatory_fees_included=True,
        passenger_age=22,
        passenger_student=True,
        passenger_eligibility_confirmed=True,
        bookable=True,
        is_teaser=False,
        observed_at=OBSERVED_AT,
        outbound_segments=(outbound,),
        return_segments=(inbound,),
        booking_components=(component,),
        expires_at=OBSERVED_AT + timedelta(minutes=30),
    )


def _flixbus_displayed_fare() -> NormalizedOffer:
    valid = _offer()
    component = replace(
        valid.booking_components[0],
        seller="FlixBus",
        source_slug="flixbus",
        booking_url=None,
        fare_name="Displayed fare",
        mandatory_fees_included=False,
        price_completeness=PriceCompleteness.ESTIMATED,
    )
    return replace(
        valid,
        source_slug="flixbus",
        price_completeness=PriceCompleteness.ESTIMATED,
        mandatory_fees_included=False,
        booking_components=(component,),
    )


class OfferTrustTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_database_engine("sqlite://")
        Base.metadata.create_all(self.engine)
        self.sessions = create_session_factory(self.engine)
        self.policy = OfferTrustPolicy({"transavia": ("transavia.com",)})

    def tearDown(self) -> None:
        self.engine.dispose()

    def test_url_policy_allows_subdomains_and_removes_transient_data(self) -> None:
        sanitized = sanitize_booking_url(
            _offer().booking_components[0].booking_url,
            ("transavia.com",),
        )
        self.assertEqual(
            sanitized,
            "https://booking.transavia.com/search?from=ORY",
        )

        unsafe = (
            "http://www.transavia.com/search",
            "https://transavia.com:444/search",
            "https://user:secret@transavia.com/search",
            "https://transavia.example/search",
            "https://transavia.com/session/abcdefghijklmnop",
        )
        for url in unsafe:
            with self.subTest(url=url), self.assertRaises(OfferRejected):
                sanitize_booking_url(url, ("transavia.com",))

    def test_valid_adult_fallback_for_student_profile_is_trusted(self) -> None:
        with self.sessions() as session:
            trusted = self.policy.validate(
                session,
                offer=_offer(),
                passenger=PassengerProfile(age=22, student=True),
            )
        self.assertEqual(trusted.total_eur_cents, 10_000)
        self.assertFalse(trusted.bookings[0].component.student_fare_applied)
        self.assertNotIn("session", trusted.bookings[0].booking_url.lower())

    def test_flixbus_displayed_fare_is_the_only_estimated_exception(self) -> None:
        with self.sessions() as session:
            trusted = self.policy.validate(
                session,
                offer=_flixbus_displayed_fare(),
                passenger=PassengerProfile(age=22, student=True),
            )
        self.assertEqual(trusted.total_eur_cents, 10_000)
        self.assertIsNone(trusted.bookings[0].booking_url)
        self.assertEqual(
            trusted.bookings[0].component.price_completeness,
            PriceCompleteness.ESTIMATED,
        )
        self.assertFalse(trusted.bookings[0].component.mandatory_fees_included)

        non_flixbus = replace(
            _flixbus_displayed_fare(),
            source_slug="transavia",
            booking_components=(
                replace(
                    _flixbus_displayed_fare().booking_components[0],
                    source_slug="transavia",
                ),
            ),
        )
        with self.sessions() as session, self.assertRaisesRegex(
            OfferRejected,
            "only complete prices",
        ):
            self.policy.validate(
                session,
                offer=non_flixbus,
                passenger=PassengerProfile(age=22, student=True),
            )

    def test_flixbus_displayed_fare_requires_aligned_component_evidence(self) -> None:
        valid = _flixbus_displayed_fare()
        component = valid.booking_components[0]
        mismatches = (
            replace(
                component,
                price_completeness=PriceCompleteness.COMPLETE,
                mandatory_fees_included=True,
            ),
            replace(component, price_completeness=PriceCompleteness.COMPLETE),
            replace(component, mandatory_fees_included=True),
            replace(component, source_slug="transavia"),
        )

        with self.sessions() as session:
            for mismatched_component in mismatches:
                with self.subTest(component=mismatched_component), self.assertRaises(
                    OfferRejected
                ) as captured:
                    self.policy.validate(
                        session,
                        offer=replace(
                            valid,
                            booking_components=(mismatched_component,),
                        ),
                        passenger=PassengerProfile(age=22, student=True),
                    )
                self.assertEqual(
                    captured.exception.code,
                    "displayed_fare_component_mismatch",
                )

    def test_fees_eligibility_teasers_and_non_bookable_fares_are_excluded(self) -> None:
        valid = _offer()
        invalid = (
            replace(valid, mandatory_fees_included=False),
            replace(valid, passenger_eligibility_confirmed=False),
            replace(valid, passenger_age=23),
            replace(valid, is_teaser=True),
            replace(valid, bookable=False),
            replace(
                valid,
                booking_components=(
                    replace(
                        valid.booking_components[0],
                        mandatory_fees_included=False,
                    ),
                ),
            ),
        )
        with self.sessions() as session:
            for offer in invalid:
                with self.subTest(offer=offer), self.assertRaises(OfferRejected):
                    self.policy.validate(
                        session,
                        offer=offer,
                        passenger=PassengerProfile(age=22, student=True),
                    )

    def test_latest_prior_published_fx_rate_is_used(self) -> None:
        with self.sessions() as session, session.begin():
            session.add_all(
                (
                    FxRate(
                        rate_date=date(2026, 12, 30),
                        currency="GBP",
                        eur_per_unit=1.10,
                        published_at=datetime(2026, 12, 30, 15, tzinfo=UTC),
                    ),
                    FxRate(
                        rate_date=date(2026, 12, 31),
                        currency="GBP",
                        eur_per_unit=1.15,
                        published_at=datetime(2026, 12, 31, 15, tzinfo=UTC),
                    ),
                    FxRate(
                        rate_date=date(2027, 1, 1),
                        currency="GBP",
                        eur_per_unit=9.99,
                        published_at=datetime(2027, 1, 1, 15, tzinfo=UTC),
                    ),
                )
            )
        gbp = replace(
            _offer(),
            currency="GBP",
            booking_components=(
                replace(
                    _offer().booking_components[0],
                    currency="GBP",
                ),
            ),
        )
        with self.sessions() as session:
            trusted = self.policy.validate(
                session,
                offer=gbp,
                passenger=PassengerProfile(),
            )
        self.assertEqual(trusted.total_eur_cents, 11_500)
        self.assertEqual(trusted.bookings[0].fx_rate, 1.15)
        self.assertEqual(trusted.bookings[0].fx_rate_date, date(2026, 12, 31))

    def test_missing_and_stale_rates_fail_closed(self) -> None:
        with self.sessions() as session, session.begin():
            session.add(
                FxRate(
                    rate_date=date(2026, 12, 20),
                    currency="GBP",
                    eur_per_unit=1.15,
                    published_at=datetime(2026, 12, 20, 15, tzinfo=UTC),
                )
            )
        converter = FxRateConverter(maximum_age=timedelta(days=7))
        with self.sessions() as session:
            with self.assertRaisesRegex(OfferRejected, "too old"):
                converter.convert(
                    session,
                    amount_minor=10_000,
                    currency="GBP",
                    observed_at=OBSERVED_AT,
                )
            with self.assertRaisesRegex(OfferRejected, "no published"):
                converter.convert(
                    session,
                    amount_minor=10_000,
                    currency="CHF",
                    observed_at=OBSERVED_AT,
                )


if __name__ == "__main__":
    unittest.main()
