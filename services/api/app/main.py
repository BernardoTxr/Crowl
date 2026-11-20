from __future__ import annotations

import logging
from collections.abc import Callable
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from crowl_core.catalog import seed_development_data
from crowl_core.db import Base, create_database_engine, create_session_factory
from crowl_core.models import City  # noqa: F401 - imports and registers model metadata

from .config import APISettings
from .routers import catalog, fares, health, status


logger = logging.getLogger(__name__)

Clock = Callable[[], datetime]


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def create_app(settings: APISettings | None = None, *, clock: Clock | None = None) -> FastAPI:
    resolved = settings or APISettings.from_env()
    engine = create_database_engine(resolved.database_url)
    session_factory = create_session_factory(engine)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.startup_error = None
        try:
            if resolved.auto_create_schema:
                Base.metadata.create_all(engine)
            if resolved.auto_seed:
                with session_factory() as session:
                    seed_development_data(
                        session,
                        data_dir=resolved.data_dir,
                        include_fixture_fares=resolved.seed_fixtures,
                    )
        except Exception as exc:  # readiness remains available with sanitized output
            app.state.startup_error = type(exc).__name__
            logger.exception("Crowl API database/catalog initialization failed")
        yield
        engine.dispose()

    app = FastAPI(
        title="Crowl API",
        version="0.1.0",
        description="Read-only historical weekend-fare API",
        lifespan=lifespan,
    )
    app.state.settings = resolved
    app.state.clock = clock or utc_now
    app.state.engine = engine
    app.state.session_factory = session_factory
    app.state.startup_error = None

    if resolved.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(resolved.cors_origins),
            allow_credentials=False,
            allow_methods=["GET", "HEAD", "OPTIONS"],
            allow_headers=["Accept", "Content-Type"],
        )

    app.include_router(health.router)
    app.include_router(catalog.router, prefix="/api/v1")
    app.include_router(fares.router, prefix="/api/v1")
    app.include_router(status.router, prefix="/api/v1")
    return app


app = create_app()
