from __future__ import annotations

import copy
import importlib.util
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "crowl_validate_data",
    ROOT / "scripts" / "validate_data.py",
)
assert SPEC and SPEC.loader
validate_data = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(validate_data)


def test_checked_in_mock_catalog_is_production_valid() -> None:
    catalog = validate_data.validate_city_catalog(production=True)
    validate_data.validate_terminals(catalog)
    validate_data.validate_sources()


def test_duplicate_definitions_are_rejected_before_set_conversion() -> None:
    with pytest.raises(validate_data.ValidationError, match="duplicate category definitions"):
        validate_data.unique(["culture", "culture"], "category definitions")


def test_production_photo_rejects_hotlinks_and_missing_attribution() -> None:
    catalog = validate_data.validate_city_catalog(production=False)
    city = catalog["cities"][0]
    hotlinked = copy.deepcopy(city["photo"])
    hotlinked["asset"] = "https://images.example/amsterdam.webp"
    with pytest.raises(validate_data.ValidationError, match="root-relative local asset"):
        validate_data.validate_production_photo(city["id"], hotlinked)

    unattributed = copy.deepcopy(city["photo"])
    unattributed["attribution"] = None
    with pytest.raises(validate_data.ValidationError, match="provenance or attribution"):
        validate_data.validate_production_photo(city["id"], unattributed)
