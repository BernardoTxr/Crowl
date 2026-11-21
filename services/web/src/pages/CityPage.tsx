import { useEffect, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { Link, useParams, useSearchParams } from 'react-router-dom';
import { api } from '../api/client';
import { TravelFilters } from '../components/Filters';
import { ItineraryPanel } from '../components/ItineraryPanel';
import { DemoNotice, ErrorBlock, LoadingBlock } from '../components/Layout';
import { PriceHeatmap } from '../components/PriceHeatmap';
import type { HeatmapCell } from '../types';
import { categoryLabel, formatDuration, nextNonpastWeekend, safeMode } from '../utils';

const durationModes = [
  { mode: 'flight', label: 'Flight', icon: '✈' },
  { mode: 'train', label: 'Train', icon: '▰' },
  { mode: 'bus', label: 'Bus', icon: '▣' },
] as const;

export const CityPage = () => {
  const { slug = '' } = useParams();
  const [searchParams, setSearchParams] = useSearchParams();
  const mode = safeMode(searchParams.get('mode'));
  const [historicalCell, setHistoricalCell] = useState<HeatmapCell | null>(null);
  const cityQuery = useQuery({ queryKey: ['city', slug], queryFn: () => api.getCity(slug) });
  const metaQuery = useQuery({ queryKey: ['meta'], queryFn: api.getMeta });
  const weekendsQuery = useQuery({ queryKey: ['weekends'], queryFn: api.getWeekends });
  const historyQuery = useQuery({ queryKey: ['history', slug, mode], queryFn: () => api.getPriceHistory(slug, mode), enabled: Boolean(slug) });
  const latestWeekend = searchParams.get('weekend') ?? nextNonpastWeekend(weekendsQuery.data?.data ?? []) ?? '';
  const inspectedWeekend = historicalCell?.weekend ?? latestWeekend;
  const inspectedRevision = historicalCell?.revisionId ?? null;
  const selectedNoRevisionState = historicalCell && !historicalCell.revisionId
    ? {
        availability: historicalCell.availability,
        coverage: historicalCell.coverage,
        freshness: historicalCell.freshness,
        observedAt: historicalCell.observedAt,
        currentObservedAt: historicalCell.currentObservedAt,
        currentCrawlRunId: historicalCell.currentCrawlRunId,
        currentDiagnostic: historicalCell.currentDiagnostic,
      }
    : null;
  const journeyQuery = useQuery({
    queryKey: ['journey', slug, inspectedWeekend, mode, inspectedRevision],
    queryFn: () => api.getJourney(slug, inspectedWeekend, mode, inspectedRevision),
    enabled: Boolean(slug && inspectedWeekend && !selectedNoRevisionState),
  });

  const city = cityQuery.data?.data;
  useEffect(() => {
    document.title = city ? `${city.name} weekend fares — Crowl` : 'City — Crowl';
  }, [city]);

  useEffect(() => {
    if (searchParams.get('mode') !== 'mixed') return;
    const next = new URLSearchParams(searchParams);
    next.delete('mode');
    setSearchParams(next, { replace: true });
  }, [searchParams, setSearchParams]);

  const updateFilter = (key: 'weekend' | 'mode', value: string) => {
    const next = new URLSearchParams(searchParams);
    if (!value || (key === 'mode' && value === 'all')) next.delete(key);
    else next.set(key, value);
    setHistoricalCell(null);
    setSearchParams(next);
  };

  if (cityQuery.isLoading || metaQuery.isLoading || weekendsQuery.isLoading) {
    return <div className="page-shell"><LoadingBlock label="Opening the city notebook…" /></div>;
  }
  if (cityQuery.error || !city) {
    return (
      <div className="page-shell narrow-page">
        <ErrorBlock message={cityQuery.error instanceof Error ? cityQuery.error.message : 'This city is not monitored.'} />
        <Link className="outline-button" to="/">Return to the map</Link>
      </div>
    );
  }

  const categoryLabels = Object.fromEntries(
    (metaQuery.data?.data.categoryDefinitions ?? []).map((definition) => [definition.id, definition.label]),
  );
  const attributeDefinitions = Object.fromEntries(
    (metaQuery.data?.data.attributeDefinitions ?? []).map((definition) => [definition.id, definition]),
  );
  const isSynthetic = Boolean(
    historyQuery.data?.isSynthetic
    || journeyQuery.data?.isSynthetic
    || city.typicalDurations?.some((duration) => duration.isSynthetic),
  );

  return (
    <div className="page-shell city-page">
      <nav className="breadcrumb" aria-label="Breadcrumb"><Link to={`/${searchParams.size ? `?${searchParams}` : ''}`}>Explore</Link><span aria-hidden="true">/</span><span aria-current="page">{city.name}</span></nav>
      {isSynthetic && <DemoNotice compact />}
      <section className="city-hero">
        <img src={city.photo.asset} alt={city.photo.alt} style={{ objectPosition: city.photo.objectPosition }} onError={(event) => { if (!event.currentTarget.src.endsWith('/crowl-mark.svg')) event.currentTarget.src = '/crowl-mark.svg'; }} />
        <div className="city-hero-overlay" />
        <div className="city-hero-content">
          <div className="chip-row">
            {city.status.visited
              ? <span className="chip chip-visited">✓ Visited</span>
              : city.status.wishlist && <span className="chip chip-wishlist">☆ Wishlist</span>}
          </div>
          <p className="eyebrow">{city.country.name}</p>
          <h1>{city.name}</h1>
          <p>{city.summary}</p>
          <div className="chip-row city-categories">{city.categories.map((category) => <span className="chip" key={category}>{categoryLabels[category] ?? categoryLabel(category)}</span>)}</div>
        </div>
        {city.photo.creator && <a className="photo-credit" href={city.photo.sourceUrl ?? undefined} target="_blank" rel="noreferrer">Photo: {city.photo.creator}</a>}
      </section>

      <section className="city-facts" aria-label={`${city.name} overview`}>
        <div className="attribute-card">
          <p className="eyebrow">City character</p>
          <h2>At a glance</h2>
          <div className="attribute-list">
            {Object.entries(city.attributes).map(([key, value]) => (
              <div className="attribute-row" key={key}>
                <span className="attribute-copy">
                  <span>{attributeDefinitions[key]?.label ?? city.attributeLabels?.[key] ?? categoryLabel(key)}</span>
                  {attributeDefinitions[key]?.lowLabel && attributeDefinitions[key]?.highLabel && <small>{attributeDefinitions[key].lowLabel} → {attributeDefinitions[key].highLabel}</small>}
                </span>
                <span className="rating-dots" aria-label={`${attributeDefinitions[key]?.label ?? categoryLabel(key)}: ${value} out of ${attributeDefinitions[key]?.max ?? 5}`}>{Array.from({ length: attributeDefinitions[key]?.max ?? 5 }, (_, index) => index + 1).map((point) => <i className={point <= value ? 'is-filled' : ''} key={point} />)}</span>
              </div>
            ))}
          </div>
        </div>
        <div className="duration-section">
          <div><p className="eyebrow">Terminal to terminal</p><h2>Typical journey time</h2></div>
          <div className="duration-grid">
            {durationModes.map((item) => {
              const duration = city.typicalDurations?.find((value) => value.mode === item.mode);
              return (
                <article className="duration-card" key={item.mode}>
                  <span aria-hidden="true">{item.icon}</span>
                  <div><strong>{item.label}</strong><b>{formatDuration(duration?.medianMinutes)}</b>{duration?.sampleCount ? <small>{duration.sampleCount} observations{duration.p25Minutes && duration.p75Minutes ? ` · ${formatDuration(duration.p25Minutes)}–${formatDuration(duration.p75Minutes)}` : ' · rolling median'}</small> : <small>Not enough observations yet</small>}</div>
                </article>
              );
            })}
          </div>
        </div>
      </section>

      <TravelFilters
        weekends={weekendsQuery.data?.data ?? []}
        weekend={latestWeekend}
        mode={mode}
        onWeekendChange={(value) => updateFilter('weekend', value)}
        onModeChange={(value) => updateFilter('mode', value)}
      />

      <section className="city-data-layout">
        <div className="history-card">
          <div className="section-heading">
            <div><p className="eyebrow">Past observations + future weekends</p><h2>Price history</h2></div>
            <p className="section-note">Columns show how many days remained before departure.</p>
          </div>
          {historyQuery.isLoading ? <LoadingBlock label="Building the price history…" /> : historyQuery.error ? <ErrorBlock message="Price history is unavailable." /> : historyQuery.data && <PriceHeatmap history={historyQuery.data.data} scheduledWeekends={(weekendsQuery.data?.data ?? []).map((weekend) => weekend.friday)} selected={historicalCell} onSelect={setHistoricalCell} />}
        </div>
        {!selectedNoRevisionState && journeyQuery.isLoading ? <LoadingBlock label="Composing the best journey…" /> : !selectedNoRevisionState && journeyQuery.error ? <ErrorBlock message="Journey details are unavailable." /> : <ItineraryPanel journey={selectedNoRevisionState ? null : journeyQuery.data?.data ?? null} emptyState={selectedNoRevisionState ?? journeyQuery.data?.emptyState} historicalCell={historicalCell} onBackToLatest={() => setHistoricalCell(null)} synthetic={Boolean(journeyQuery.data?.isSynthetic)} />}
      </section>
    </div>
  );
};
