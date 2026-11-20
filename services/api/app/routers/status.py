from __future__ import annotations

from datetime import datetime, time, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from crowl_core.enums import ExtractorRunStatus
from crowl_core.models import (
    CrawlRun,
    ExtractorRun,
    FareSnapshotRevision,
    Source,
)

from ..dependencies import get_session
from ..presenters import as_utc
from ..schemas import (
    Batch,
    Extractor,
    ExtractorCounts,
    ExtractorRunItem,
    ExtractorRunsResponse,
    ExtractorsResponse,
    LatestBatchResponse,
)


router = APIRouter(tags=["monitoring"])


def _next_batch(now: datetime) -> datetime:
    today = datetime.combine(now.date(), time(2, 0), timezone.utc)
    return today if now < today else today + timedelta(days=1)


def _request_now(request: Request) -> datetime:
    return as_utc(request.app.state.clock()) or datetime.now(timezone.utc)


def _freshness(
    last_success_at: datetime | None,
    *,
    now: datetime,
    freshness_hours: int,
) -> str:
    observed_at = as_utc(last_success_at)
    if observed_at is None:
        return "never_observed"
    if now - observed_at > timedelta(hours=freshness_hours):
        return "stale"
    return "fresh"


def _latest_batch(session: Session) -> CrawlRun | None:
    return session.scalar(
        select(CrawlRun)
        .where(CrawlRun.is_synthetic.is_(False))
        .order_by(CrawlRun.scheduled_for.desc(), CrawlRun.id.desc())
        .limit(1)
    )


def _counts(row: ExtractorRun | None) -> ExtractorCounts:
    return ExtractorCounts(
        expected=row.expected_searches if row else 0,
        attempted=row.attempted_searches if row else 0,
        successful=row.successful_searches if row else 0,
        empty=row.empty_searches if row else 0,
        result_bearing=row.result_searches if row else 0,
        offers=row.offers_found if row else 0,
        physical_requests=row.physical_requests if row else 0,
    )


@router.get("/batches/latest", response_model=LatestBatchResponse)
def latest_batch(
    request: Request,
    session: Session = Depends(get_session),
) -> LatestBatchResponse:
    now = _request_now(request)
    row = _latest_batch(session)
    if row is None:
        return LatestBatchResponse(
            batch=None,
            state="not_started",
            next_expected_batch_at=_next_batch(now),
        )
    latest_observation = session.scalar(
        select(func.max(FareSnapshotRevision.observed_at))
        .join(CrawlRun, FareSnapshotRevision.crawl_run_id == CrawlRun.id)
        .where(
            FareSnapshotRevision.total_price_eur_cents.is_not(None),
            CrawlRun.is_synthetic.is_(False),
        )
    )
    expected = row.expected_cells
    coverage = min(1.0, row.completed_cells / expected) if expected else 0.0
    pulse = as_utc(row.heartbeat_at or row.completed_at or row.started_at or row.created_at)
    stale = pulse is None or now - pulse > timedelta(
        hours=request.app.state.settings.freshness_hours
    )
    sources = session.scalars(select(Source).where(Source.is_requested.is_(True))).all()
    return LatestBatchResponse(
        batch=Batch(
            id=row.id,
            scheduled_for=row.scheduled_for,
            status=row.status,
            started_at=as_utc(row.started_at),
            heartbeat_at=as_utc(row.heartbeat_at),
            completed_at=as_utc(row.completed_at),
            published_at=as_utc(row.published_at),
            expected_cells=expected,
            completed_cells=row.completed_cells,
            successful_cells=row.successful_cells,
            requested_sources=len(sources),
            enabled_sources=sum(source.enabled for source in sources),
            execution_coverage_ratio=coverage,
            latest_trustworthy_observation_at=as_utc(latest_observation),
            diagnostic=row.diagnostic,
        ),
        state="stale" if stale else "current",
        next_expected_batch_at=_next_batch(now),
    )


@router.get("/extractors", response_model=ExtractorsResponse)
def extractors(
    request: Request,
    session: Session = Depends(get_session),
) -> ExtractorsResponse:
    now = _request_now(request)
    batch = _latest_batch(session)
    sources = session.scalars(
        select(Source).where(Source.is_requested.is_(True)).order_by(Source.name)
    ).all()
    runs: dict[str, ExtractorRun] = {}
    if batch:
        runs = {
            run.source_id: run
            for run in session.scalars(
                select(ExtractorRun).where(ExtractorRun.crawl_run_id == batch.id)
            ).all()
        }
    enabled_count = sum(source.enabled for source in sources)
    scope = enabled_count / len(sources) if sources else 0.0
    last_success_by_source = dict(
        session.execute(
            select(ExtractorRun.source_id, func.max(ExtractorRun.last_success_at))
            .where(ExtractorRun.source_id.in_([source.slug for source in sources]))
            .group_by(ExtractorRun.source_id)
        ).all()
    )
    items: list[Extractor] = []
    for source in sources:
        row = runs.get(source.slug)
        last_success_at = as_utc(last_success_by_source.get(source.slug))
        if not source.enabled:
            state = ExtractorRunStatus.DISABLED
        elif row is None:
            state = ExtractorRunStatus.PENDING
        else:
            state = row.status
        expected = row.expected_searches if row else 0
        coverage = min(1.0, row.attempted_searches / expected) if expected and row else 0.0
        items.append(
            Extractor(
                source=source.slug,
                display_name=source.name,
                modes=list(source.supported_modes),
                strategy=source.strategy,
                connector_version=source.connector_version,
                parser_version=source.parser_version,
                enabled=source.enabled,
                disabled_reason=source.disabled_reason,
                state=state,
                last_attempt_at=as_utc(
                    row.last_response_at or row.completed_at or row.started_at if row else None
                ),
                last_success_at=last_success_at,
                freshness=_freshness(
                    last_success_at,
                    now=now,
                    freshness_hours=request.app.state.settings.freshness_hours,
                ),
                runtime_ms=row.runtime_ms if row else None,
                consecutive_failures=row.consecutive_failures if row else 0,
                execution_coverage_ratio=coverage,
                source_scope_ratio=scope,
                counts=_counts(row),
                diagnostic=row.diagnostic if row else source.disabled_reason,
            )
        )
    return ExtractorsResponse(
        batch_id=batch.id if batch else None,
        requested_sources=len(sources),
        enabled_sources=enabled_count,
        items=items,
    )


@router.get("/extractors/{source}/runs", response_model=ExtractorRunsResponse)
def extractor_runs(
    source: str,
    limit: int = Query(30, ge=1, le=100),
    session: Session = Depends(get_session),
) -> ExtractorRunsResponse:
    known = session.get(Source, source)
    if known is None or not known.is_requested:
        raise HTTPException(status_code=404, detail="source_not_found")
    rows = session.execute(
        select(ExtractorRun, CrawlRun)
        .join(CrawlRun, ExtractorRun.crawl_run_id == CrawlRun.id)
        .where(
            ExtractorRun.source_id == source,
            CrawlRun.is_synthetic.is_(False),
        )
        .order_by(CrawlRun.scheduled_for.desc(), ExtractorRun.id.desc())
        .limit(limit)
    ).all()
    return ExtractorRunsResponse(
        source=source,
        items=[
            ExtractorRunItem(
                id=row.id,
                batch_id=batch.id,
                scheduled_for=batch.scheduled_for,
                state=row.status,
                started_at=as_utc(row.started_at),
                completed_at=as_utc(row.completed_at),
                last_success_at=as_utc(row.last_success_at),
                runtime_ms=row.runtime_ms,
                consecutive_failures=row.consecutive_failures,
                counts=_counts(row),
                diagnostic=row.diagnostic,
            )
            for row, batch in rows
        ],
    )
