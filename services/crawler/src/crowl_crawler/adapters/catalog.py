"""Load provider slot metadata from the version-controlled source catalog."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from crowl_core.catalog import CatalogValidationError, load_source_catalog

from ..models import ExtractionStrategy, TransportMode


SOURCE_CATALOG_ENV = "CROWL_SOURCE_CATALOG"
DATA_DIRECTORY_ENV = "CROWL_DATA_DIR"
_CONTROL_CHARACTERS = re.compile(r"[\x00-\x1f\x7f]+")


@dataclass(frozen=True, slots=True)
class SourceSpec:
    """Typed view of one entry in ``data/sources.yaml``.

    The catalog owns feasibility, intended extraction strategy, operational
    state, and the emergency kill switch. Provider modules only bind a source
    id to an implementation class; they do not repeat this metadata.
    """

    slug: str
    name: str
    modes: tuple[TransportMode, ...]
    intended_strategy: ExtractionStrategy
    access_basis: str
    disabled_reason: str
    state: str
    configured_enabled: bool
    kill_switch: bool
    config_version: str
    booking_hosts: tuple[str, ...]

    @property
    def catalog_enabled(self) -> bool:
        """Whether the catalog permits a live connector to execute."""

        return (
            self.configured_enabled
            and self.state == "enabled"
            and not self.kill_switch
        )

    @classmethod
    def from_catalog(cls, entry: Mapping[str, Any]) -> SourceSpec:
        slug = str(entry["id"])
        name = str(entry["name"])
        modes = tuple(TransportMode(value) for value in entry["modes"])
        intended_strategy = ExtractionStrategy(str(entry["strategy"]))
        state = str(entry["state"])
        configured_enabled = bool(entry["enabled"])
        kill_switch = bool(entry["kill_switch"])
        access_note = _public_text(
            entry["access_note"],
            fallback="Public-page extraction is disabled.",
        )

        if kill_switch:
            disabled_reason = f"Catalog kill switch is active. {access_note}"
        elif not configured_enabled or state != "enabled":
            disabled_reason = access_note
        else:
            disabled_reason = "Public-page extraction is enabled by the catalog."

        return cls(
            slug=slug,
            name=name,
            modes=modes,
            intended_strategy=intended_strategy,
            access_basis=_public_text(
                entry["access_basis"],
                fallback="ordinary public provider page",
            ),
            disabled_reason=disabled_reason,
            state=state,
            configured_enabled=configured_enabled,
            kill_switch=kill_switch,
            config_version=_public_text(entry["config_version"], fallback="unknown"),
            booking_hosts=tuple(str(host) for host in entry["booking_hosts"]),
        )


def source_catalog_path(path: str | Path | None = None) -> Path:
    """Resolve ``sources.yaml`` for local, Docker, and Railway execution."""

    if path is not None:
        return Path(path).expanduser().resolve()

    explicit = os.environ.get(SOURCE_CATALOG_ENV, "").strip()
    if explicit:
        return Path(explicit).expanduser().resolve()

    data_directory = os.environ.get(DATA_DIRECTORY_ENV, "").strip()
    if data_directory:
        return (Path(data_directory).expanduser() / "sources.yaml").resolve()

    candidates = [Path.cwd() / "data" / "sources.yaml", Path("/app/data/sources.yaml")]
    candidates.extend(
        parent / "data" / "sources.yaml"
        for parent in Path(__file__).resolve().parents
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()

    raise CatalogValidationError(
        "source catalog not found; set CROWL_DATA_DIR or CROWL_SOURCE_CATALOG"
    )


def load_source_specs(path: str | Path | None = None) -> tuple[SourceSpec, ...]:
    entries = load_source_catalog(source_catalog_path(path))
    if len(entries) != 14:
        raise CatalogValidationError(
            "Crowl's requested source catalog must contain exactly 14 entries"
        )
    return tuple(SourceSpec.from_catalog(entry) for entry in entries)


def _public_text(value: object, *, fallback: str) -> str:
    """Normalize catalog text before it can appear in status output."""

    if not isinstance(value, str):
        return fallback
    normalized = " ".join(_CONTROL_CHARACTERS.sub(" ", value).split())
    return normalized[:500] or fallback
