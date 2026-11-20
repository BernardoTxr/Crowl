"""Shared domain and persistence primitives for Crowl."""

from .db import Base, create_database_engine, create_session_factory
from .composition import (
    ComposedRoundTrip,
    CompositionLimits,
    CompositionResult,
    OneWayCandidate,
    compose_round_trips,
)
from .travel_windows import TravelWindowSpec, generate_travel_windows

__all__ = [
    "Base",
    "ComposedRoundTrip",
    "CompositionLimits",
    "CompositionResult",
    "OneWayCandidate",
    "TravelWindowSpec",
    "create_database_engine",
    "create_session_factory",
    "compose_round_trips",
    "generate_travel_windows",
]

__version__ = "0.1.0"
