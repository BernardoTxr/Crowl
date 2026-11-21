import { useEffect, useMemo, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { useSearchParams } from 'react-router-dom';
import { api } from '../api/client';
import { DemoNotice, ErrorBlock, LoadingBlock } from '../components/Layout';
import { StatusPill } from '../components/StatusPill';
import type { Extractor, ExtractorState } from '../types';
import { batchLabel, formatDateTime, formatDuration, freshnessLabel, joinClassNames, modeLabel } from '../utils';

const extractorTone = (state: ExtractorState): 'success' | 'warning' | 'neutral' | 'danger' => state === 'succeeded' ? 'success' : state === 'partial' || state === 'running' ? 'warning' : state === 'pending' || state === 'disabled' ? 'neutral' : 'danger';
const extractorLabel = (state: ExtractorState) => state.replace('_', ' ').replace(/^\w/, (letter) => letter.toUpperCase());
const formatRuntime = (seconds: number | null) => seconds === null
  ? 'Not available'
  : seconds < 60
    ? `${new Intl.NumberFormat('en-GB', { maximumFractionDigits: 1 }).format(seconds)}s`
    : formatDuration(Math.round(seconds / 60));

export const StatusPage = () => {
  const [mode, setMode] = useState<'all' | 'flight' | 'train' | 'bus'>('all');
  const [searchParams, setSearchParams] = useSearchParams();
  const batchQuery = useQuery({ queryKey: ['latest-batch'], queryFn: api.getLatestBatch, refetchInterval: 60_000 });
  const extractorsQuery = useQuery({ queryKey: ['extractors'], queryFn: api.getExtractors, refetchInterval: 60_000 });
  const extractors = extractorsQuery.data?.data ?? [];
  const requestedSource = searchParams.get('source');
  const source = requestedSource && extractors.some((extractor) => extractor.source === requestedSource)
    ? requestedSource
    : 'all';
  const visibleExtractors = useMemo(
    () => extractors.filter((extractor) => (mode === 'all' || extractor.modes.includes(mode)) && (source === 'all' || extractor.source === source)),
    [extractors, mode, source],
  );
  const batch = batchQuery.data?.data;
  const healthy = extractors.filter((item) => item.state === 'succeeded' && item.freshness === 'fresh').length;
  const enabledSources = extractors.filter((item) => item.enabled).length;
  const isSynthetic = Boolean(batchQuery.data?.isSynthetic);

  const selectSource = (nextSource: string) => {
    const next = new URLSearchParams(searchParams);
    if (nextSource === 'all') next.delete('source');
    else next.set('source', nextSource);
    setSearchParams(next);
  };

  useEffect(() => { document.title = 'Source health — Crowl'; }, []);

  return (
    <div className="page-shell status-page">
      <header className="page-intro">
        <div><p className="eyebrow">Read-only monitoring</p><h1>Source health</h1><p>See what Crowl searched, what succeeded, and where coverage is incomplete. There are no configuration or retry controls here.</p></div>
        <div className="status-intro-icon" aria-hidden="true">◉</div>
      </header>
      {isSynthetic && <DemoNotice />}
      {(batchQuery.isLoading || extractorsQuery.isLoading) && <LoadingBlock label="Checking the latest collection batch…" />}
      {(batchQuery.error || extractorsQuery.error) && <ErrorBlock message="Monitoring data is temporarily unavailable." />}
      {batch && (
        <section className="batch-card" aria-labelledby="batch-title">
          <div className="batch-primary">
            <p className="eyebrow">Latest daily batch</p>
            <div className="batch-title-row">
              <h2 id="batch-title">{batch.state === 'stale' ? 'Stale batch' : batchLabel[batch.status]}</h2>
              <StatusPill
                label={batch.state === 'stale' ? 'Stale · degraded' : batch.status}
                tone={batch.state === 'stale' ? 'warning' : batch.status === 'succeeded' ? 'success' : batch.status === 'partial' || batch.status === 'running' ? 'warning' : 'danger'}
              />
            </div>
            <p>Scheduled for {batch.scheduledFor}</p>
          </div>
          <div className="batch-progress">
            {batch.expectedCells > 0 ? <>
              <div className="progress-copy"><span>Execution coverage</span><strong>{Math.round((batch.completedCells / batch.expectedCells) * 100)}%</strong></div>
              <div className="progress-track" role="progressbar" aria-label="Execution coverage" aria-valuenow={batch.completedCells} aria-valuemin={0} aria-valuemax={batch.expectedCells}><i style={{ width: `${Math.min(100, (batch.completedCells / batch.expectedCells) * 100)}%` }} /></div>
              <small>{batch.completedCells.toLocaleString()} of {batch.expectedCells.toLocaleString()} expected logical searches</small>
            </> : <div className="no-applicable-work" role="status">
              <span>Execution coverage</span>
              <strong>No applicable enabled work</strong>
              <small>No enabled source had a logical search scheduled for this batch.</small>
            </div>}
          </div>
          <dl className="batch-details">
            <div><dt>Sources in scope</dt><dd>{extractors.length ? enabledSources : batch.enabledSources} enabled / {extractors.length || batch.knownSources} requested</dd></div>
            <div><dt>Started</dt><dd>{formatDateTime(batch.startedAt)}</dd></div>
            <div><dt>Last heartbeat</dt><dd>{formatDateTime(batch.heartbeatAt)}</dd></div>
            <div><dt>Completed</dt><dd>{formatDateTime(batch.completedAt)}</dd></div>
            <div><dt>Latest trustworthy fare</dt><dd>{formatDateTime(batch.latestTrustworthyObservationAt)}</dd></div>
            <div><dt>Next expected batch</dt><dd>{formatDateTime(batch.nextExpectedAt)}</dd></div>
          </dl>
        </section>
      )}
      {!batchQuery.isLoading && !batchQuery.error && !batch && (
        <div className="empty-state status-empty"><strong>No daily batch has run yet</strong><span>Crowl will report coverage after the first scheduled collection.</span></div>
      )}

      <section className="extractors-section" aria-labelledby="extractors-title">
        <div className="section-heading">
          <div><p className="eyebrow">14 requested platforms</p><h2 id="extractors-title">Extractor coverage</h2></div>
          <div className="health-count"><strong>{healthy}</strong><span>healthy now</span></div>
        </div>
        <div className="status-tabs" role="group" aria-label="Filter extractors by transport">
          {(['all', 'flight', 'train', 'bus'] as const).map((item) => <button type="button" className={joinClassNames(mode === item && 'is-active')} onClick={() => setMode(item)} aria-pressed={mode === item} key={item}>{item === 'all' ? 'All sources' : modeLabel(item)}</button>)}
        </div>
        <label className="source-filter">
          <span>Source</span>
          <select aria-label="Filter by source" value={source} onChange={(event) => selectSource(event.target.value)}>
            <option value="all">All requested sources</option>
            {extractors.map((extractor) => <option value={extractor.source} key={extractor.source}>{extractor.displayName}</option>)}
          </select>
        </label>
        <p className="results-count" aria-live="polite">Showing {visibleExtractors.length} sources</p>
        <div className="extractor-list">
          {visibleExtractors.map((extractor) => <ExtractorRow extractor={extractor} defaultOpen={source === extractor.source} key={`${extractor.source}:${source}`} />)}
        </div>
        {!extractorsQuery.isLoading && !extractorsQuery.error && !visibleExtractors.length && <div className="empty-state"><strong>No matching extractors</strong><span>No requested source is configured for this transport filter.</span></div>}
      </section>
    </div>
  );
};

const ExtractorRow = ({ extractor, defaultOpen = false }: { extractor: Extractor; defaultOpen?: boolean }) => {
  const completion = extractor.counts.expected ? Math.round((extractor.counts.successful / extractor.counts.expected) * 100) : 0;
  const coverageLabel = !extractor.enabled
    ? 'Not enabled'
    : extractor.counts.expected > 0
      ? `${completion}% complete`
      : 'No applicable enabled work';
  return (
    <details className="extractor-card" open={defaultOpen || undefined}>
      <summary>
        <div className="source-identity"><span className={`source-icon source-${extractor.modes[0]}`} aria-hidden="true">{extractor.modes[0] === 'flight' ? '✈' : extractor.modes[0] === 'train' ? '▰' : '▣'}</span><div><strong>{extractor.displayName}</strong><span>{extractorLabel(extractor.state)} · {freshnessLabel[extractor.freshness]} · {extractor.strategy} · connector {extractor.version}</span></div></div>
        <StatusPill label={extractorLabel(extractor.state)} tone={extractorTone(extractor.state)} />
        <div className="source-coverage"><span>{coverageLabel}</span><div><i style={{ width: `${completion}%` }} /></div></div>
        <span className="details-chevron" aria-hidden="true">⌄</span>
      </summary>
      <div className="extractor-detail">
        <dl>
          <div><dt>Last attempt</dt><dd>{formatDateTime(extractor.lastAttemptAt)}</dd></div>
          <div><dt>Last valid success</dt><dd>{formatDateTime(extractor.lastSuccessAt)}</dd></div>
          <div><dt>Runtime</dt><dd>{formatRuntime(extractor.runtimeSeconds)}</dd></div>
          <div><dt>Consecutive failures</dt><dd>{extractor.consecutiveFailures}</dd></div>
          <div><dt>Source scope</dt><dd>{Math.round(extractor.sourceScopeRatio * 100)}%</dd></div>
          <div><dt>Freshness</dt><dd>{freshnessLabel[extractor.freshness]}</dd></div>
        </dl>
        <div className="count-grid">
          <span><b>{extractor.counts.expected.toLocaleString()}</b>Expected</span><span><b>{extractor.counts.attempted.toLocaleString()}</b>Attempted</span><span><b>{extractor.counts.successful.toLocaleString()}</b>Successful</span><span><b>{extractor.counts.empty.toLocaleString()}</b>Valid empty</span><span><b>{extractor.counts.withResults.toLocaleString()}</b>With results</span><span><b>{extractor.counts.physicalRequests.toLocaleString()}</b>Requests</span>
        </div>
        <div className="diagnostic-note"><strong>Sanitized diagnostic</strong><p>{extractor.disabledReason ?? extractor.diagnostic ?? 'No diagnostic was recorded.'}</p></div>
      </div>
    </details>
  );
};
