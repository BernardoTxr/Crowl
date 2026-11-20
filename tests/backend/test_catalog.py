from __future__ import annotations

import copy
import json
import shutil
from datetime import date
from pathlib import Path

import pytest
from sqlalchemy import func, select

from crowl_core.catalog import (
    CatalogValidationError,
    SOURCE_DEFINITIONS,
    ensure_default_profile_and_rules,
    load_city_catalog,
    seed_development_data,
    seed_fixture_history,
    validate_city_catalog,
)
from crowl_core.enums import LinkRelationship
from crowl_core.db import Base, create_database_engine, create_session_factory
from crowl_core.models import (
    City,
    CityTerminalLink,
    CrawlRun,
    FareSnapshot,
    FareSnapshotRevision,
    OriginTerminal,
    RuleVersion,
    Source,
    Terminal,
    TerminalTransfer,
    TravelWindow,
)
from crowl_core.travel_windows import generate_travel_windows


ROOT = Path(__file__).resolve().parents[2]


def test_mock_catalog_and_terminal_registry_seed_idempotently(tmp_path: Path) -> None:
    engine = create_database_engine(f"sqlite:///{tmp_path / 'catalog.db'}")
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    with factory() as session:
        first = seed_development_data(
            session,
            data_dir=ROOT / "data",
            as_of=date(2026, 1, 5),
            include_fixture_fares=True,
        )
        second = seed_development_data(
            session,
            data_dir=ROOT / "data",
            as_of=date(2026, 1, 5),
            include_fixture_fares=True,
        )

        city_payload = load_city_catalog(ROOT / "data" / "cities.mock.json")
        terminal_payload = __import__("json").loads(
            (ROOT / "data" / "terminals.json").read_text(encoding="utf-8")
        )

        assert first.cities == len(city_payload["cities"])
        assert first.terminals == len(terminal_payload["terminals"])
        assert first.sources == 14
        assert first.fixture_snapshots > first.cities
        assert second.fixture_snapshots == 0
        assert session.scalar(select(func.count()).select_from(City)) == first.cities
        assert session.scalar(select(func.count()).select_from(Terminal)) == first.terminals
        assert session.scalar(select(func.count()).select_from(CityTerminalLink)) == len(
            terminal_payload["cityTerminalLinks"]
        )
        assert session.scalar(select(func.count()).select_from(OriginTerminal)) == len(
            terminal_payload["originTerminalIds"]
        )
        assert session.scalar(select(func.count()).select_from(FareSnapshot)) == first.fixture_snapshots
        requested = {
            row.slug
            for row in session.scalars(select(Source).where(Source.is_requested.is_(True)))
        }
        assert requested == {
            definition["slug"] for definition in SOURCE_DEFINITIONS
        }
        fixture = session.get(Source, "fixture")
        assert fixture is not None and not fixture.is_requested


def test_fixture_history_tracks_the_current_full_horizon_in_an_aged_database(
    tmp_path: Path,
) -> None:
    engine = create_database_engine(f"sqlite:///{tmp_path / 'aged-catalog.db'}")
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    first_date = date(2026, 1, 5)
    current_date = date(2026, 10, 5)

    with factory() as session:
        seed_development_data(
            session,
            data_dir=ROOT / "data",
            as_of=first_date,
            include_fixture_fares=True,
        )
        seed_development_data(
            session,
            data_dir=ROOT / "data",
            as_of=current_date,
            include_fixture_fares=True,
        )

        current_fridays = {
            item.friday for item in generate_travel_windows(current_date, months_ahead=6)
        }
        covered_fridays = set(
            session.scalars(
                select(TravelWindow.friday_date)
                .join(FareSnapshot, FareSnapshot.travel_window_id == TravelWindow.id)
                .where(
                    FareSnapshot.observed_on == current_date,
                    TravelWindow.friday_date.in_(current_fridays),
                )
                .distinct()
            ).all()
        )

        assert covered_fridays == current_fridays

    engine.dispose()


def test_fixture_history_does_not_mix_rule_dimensions_in_an_existing_batch(
    tmp_path: Path,
) -> None:
    engine = create_database_engine(f"sqlite:///{tmp_path / 'fixture-rules.db'}")
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    as_of = date(2026, 1, 5)

    with factory() as session:
        seed_development_data(
            session,
            data_dir=ROOT / "data",
            as_of=as_of,
            include_fixture_fares=True,
        )
        profile, original_rule = ensure_default_profile_and_rules(session)
        session.add(
            City(
                id="fixture-new-city",
                slug="fixture-new-city",
                name="Fixture New City",
                country_code="NL",
                country_name="Netherlands",
                timezone="Europe/Amsterdam",
                latitude=52.0,
                longitude=5.0,
                visited=False,
                wishlist=True,
                categories=["culture"],
                attributes={"expense": 3},
                summary="Dimension-isolation regression fixture.",
                active=True,
            )
        )
        session.add(
            CityTerminalLink(
                city_id="fixture-new-city",
                terminal_id="ams-schiphol",
                relationship_type=LinkRelationship.PRIMARY_AIRPORT,
            )
        )
        changed_rules = copy.deepcopy(original_rule.rules)
        changed_rules["test_revision"] = 2
        replacement_rule = RuleVersion(
            config_hash="f" * 64,
            version_label="fixture-rule-isolation-test",
            schengen_scope_version=original_rule.schengen_scope_version,
            strict_schengen_path=True,
            rules=changed_rules,
        )
        session.add(replacement_rule)
        session.flush()

        created = seed_fixture_history(
            session,
            as_of=as_of,
            profile=profile,
            rule=replacement_rule,
        )
        session.commit()

        assert created == 0
        assert session.scalar(
            select(func.count())
            .select_from(FareSnapshot)
            .where(FareSnapshot.destination_city_id == "fixture-new-city")
        ) == 0
        dimension_mismatches = session.scalar(
            select(func.count())
            .select_from(FareSnapshotRevision)
            .join(CrawlRun, FareSnapshotRevision.crawl_run_id == CrawlRun.id)
            .where(
                (FareSnapshotRevision.rule_version_id != CrawlRun.rule_version_id)
                | (FareSnapshotRevision.profile_id != CrawlRun.profile_id)
            )
        )
        assert dimension_mismatches == 0

    engine.dispose()


def test_catalog_semantic_validation_rejects_unknown_category_and_non_schengen() -> None:
    payload = load_city_catalog(ROOT / "data" / "cities.mock.json")
    broken = copy.deepcopy(payload)
    broken["cities"][0]["categories"].append("does-not-exist")
    with pytest.raises(CatalogValidationError, match="unknown categories"):
        validate_city_catalog(broken)

    broken = copy.deepcopy(payload)
    broken["cities"][0]["country"]["code"] = "GB"
    with pytest.raises(CatalogValidationError, match="outside configured Schengen"):
        validate_city_catalog(broken)


def test_incomplete_rule_identity_cannot_be_created_without_catalog(tmp_path: Path) -> None:
    engine = create_database_engine(f"sqlite:///{tmp_path / 'unseeded.db'}")
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    with factory() as session, pytest.raises(CatalogValidationError, match="must be seeded"):
        ensure_default_profile_and_rules(session)


def test_rule_identity_and_catalog_reconciliation_are_authoritative(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    shutil.copytree(ROOT / "data", data_dir)
    engine = create_database_engine(f"sqlite:///{tmp_path / 'rules.db'}")
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)

    with factory() as session:
        seed_development_data(session, data_dir=data_dir, as_of=date(2026, 1, 5))
        original_rule = session.scalar(select(RuleVersion))
        assert original_rule is not None
        original_rule_id = original_rule.id
        original_rule_payload = copy.deepcopy(original_rule.rules)
        assert original_rule.schengen_scope_version.startswith("2026-11-20-")
        assert original_rule.rules["catalog"]["schengen"]["source"].startswith("https://")
        assert original_rule.rules["catalog"]["terminal_catalog"][
            "origin_terminal_ids"
        ] == sorted(
            json.loads((data_dir / "terminals.json").read_text())["originTerminalIds"]
        )

        # Simulate unsafe operational drift. A catalog reconciliation must
        # restore its explicit kill switches and exact current relationships.
        source = session.get(Source, "ryanair")
        assert source is not None
        source.enabled = True
        source.disabled_reason = None
        session.add(
            Source(
                slug="removed_source",
                name="Removed source",
                supported_modes=["flight"],
                strategy="http_structured",
                is_requested=True,
                enabled=True,
                connector_version="live",
                parser_version="live",
            )
        )
        session.add(
            OriginTerminal(
                rule_version_id=original_rule.id,
                terminal_id="ams-schiphol",
                permits_departure=True,
                permits_return=True,
            )
        )
        session.add(
            TerminalTransfer(
                from_terminal_id="par-gare-du-nord",
                to_terminal_id="par-gare-de-l-est",
                minimum_minutes=20,
                cost_eur_cents=0,
                cost_is_complete=True,
            )
        )
        session.commit()

        seed_development_data(session, data_dir=data_dir, as_of=date(2026, 1, 5))
        assert session.scalar(select(func.count()).select_from(RuleVersion)) == 1
        assert session.scalar(
            select(func.count())
            .select_from(OriginTerminal)
            .where(OriginTerminal.rule_version_id == original_rule_id)
        ) == len(json.loads((data_dir / "terminals.json").read_text())["originTerminalIds"])
        assert session.get(Source, "ryanair").enabled is False
        removed = session.get(Source, "removed_source")
        assert removed is not None and not removed.enabled and not removed.is_requested
        assert session.scalar(select(func.count()).select_from(TerminalTransfer)) == 0

        terminal_payload = json.loads((data_dir / "terminals.json").read_text())
        terminal_payload["originTerminalIds"].remove("par-bva")
        terminal_payload["terminalTransfers"] = [
            {
                "fromTerminalId": "par-gare-du-nord",
                "toTerminalId": "par-gare-de-l-est",
                "minimumMinutes": 20,
                "costEurCents": 0,
                "costIsComplete": True,
                "evidenceUrl": "https://example.invalid/transfer-rule",
            }
        ]
        (data_dir / "terminals.json").write_text(json.dumps(terminal_payload), encoding="utf-8")
        seed_development_data(session, data_dir=data_dir, as_of=date(2026, 1, 5))

        rules = session.scalars(select(RuleVersion).order_by(RuleVersion.id)).all()
        assert len(rules) == 2
        assert rules[0].rules == original_rule_payload
        assert rules[0].config_hash != rules[1].config_hash
        assert session.scalar(
            select(func.count())
            .select_from(OriginTerminal)
            .where(OriginTerminal.rule_version_id == rules[1].id)
        ) == len(terminal_payload["originTerminalIds"])
        assert session.scalar(select(func.count()).select_from(TerminalTransfer)) == 1

        historical_origin = session.scalar(
            select(OriginTerminal).where(
                OriginTerminal.rule_version_id == rules[0].id,
                OriginTerminal.terminal_id == "par-bva",
            )
        )
        assert historical_origin is not None
        session.delete(historical_origin)
        session.add(
            OriginTerminal(
                rule_version_id=rules[0].id,
                terminal_id="ams-schiphol",
                permits_departure=True,
                permits_return=True,
            )
        )
        session.commit()
        seed_development_data(session, data_dir=data_dir, as_of=date(2026, 1, 5))
        assert session.scalar(
            select(func.count())
            .select_from(OriginTerminal)
            .where(OriginTerminal.rule_version_id == rules[0].id)
        ) == len(original_rule_payload["catalog"]["terminal_catalog"]["origin_terminal_ids"])
        assert session.scalar(
            select(func.count())
            .select_from(OriginTerminal)
            .where(
                OriginTerminal.rule_version_id == rules[0].id,
                OriginTerminal.terminal_id == "par-bva",
            )
        ) == 1

        # Provenance changes are semantic even when country membership is
        # unchanged, so they receive a distinct immutable rule version.
        schengen_payload = json.loads((data_dir / "schengen.json").read_text())
        schengen_payload["source"] = "https://example.invalid/revalidated-schengen-scope"
        (data_dir / "schengen.json").write_text(json.dumps(schengen_payload), encoding="utf-8")
        seed_development_data(session, data_dir=data_dir, as_of=date(2026, 1, 5))
        rules = session.scalars(select(RuleVersion).order_by(RuleVersion.id)).all()
        assert len(rules) == 3
        assert len({rule.config_hash for rule in rules}) == 3
        assert rules[2].schengen_scope_version.startswith("2026-11-20-")
        assert rules[1].schengen_scope_version != rules[2].schengen_scope_version

        # Reverting catalog files reactivates their existing immutable record;
        # publishers without filesystem inputs must not keep using the largest ID.
        shutil.copy2(ROOT / "data" / "terminals.json", data_dir / "terminals.json")
        shutil.copy2(ROOT / "data" / "schengen.json", data_dir / "schengen.json")
        seed_development_data(session, data_dir=data_dir, as_of=date(2026, 1, 5))
        _, active_rule = ensure_default_profile_and_rules(session)
        assert active_rule.id == original_rule_id
        assert session.scalar(select(func.count()).select_from(RuleVersion)) == 3
