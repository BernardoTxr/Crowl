import { useEffect, useMemo, useRef, useState, type CSSProperties, type KeyboardEvent } from 'react';
import type { HeatmapCell, PriceHistory } from '../types';
import { availabilityLabel, formatCurrency, formatDate, formatDateTime, joinClassNames } from '../utils';

interface PriceHeatmapProps {
  history: PriceHistory;
  scheduledWeekends?: string[];
  selected?: HeatmapCell | null;
  onSelect: (cell: HeatmapCell) => void;
}

const cellTone = (cell: HeatmapCell, minimum: number, maximum: number) => {
  if (cell.priceCents === null) {
    if (cell.availability === 'not_on_sale') return 'heat-not-on-sale';
    if (cell.availability === 'sold_out') return 'heat-sold-out';
    if (cell.availability === 'no_valid_option') return 'heat-no-option';
    if (cell.availability === 'unknown_due_failure') return 'heat-failed';
    return 'heat-empty';
  }
  const ratio = Math.max(0, Math.min(0.999, (cell.priceCents - minimum) / Math.max(1, maximum - minimum)));
  return `heat-${Math.floor(ratio * 5) + 1}`;
};

const cellText = (cell: HeatmapCell) => cell.priceCents === null
  ? cell.availability === 'not_on_sale' ? '·' : cell.availability === 'sold_out' ? '×' : cell.availability === 'unknown_due_failure' ? '!' : '—'
  : formatCurrency(cell.priceCents).replace(/\.00$/, '');

const fallbackLeads = [180, 150, 120, 90, 60, 30, 14, 7, 0];
const MAX_HEATMAP_COLUMNS = 18;
const DEFAULT_PAST_WEEKENDS = 26;
const DEFAULT_UPCOMING_WEEKENDS = 26;
const TABLE_PAGE_SIZE = 12;

const tripHasEnded = (weekend: string, today: number) => {
  const end = new Date(`${weekend}T12:00:00Z`);
  end.setUTCDate(end.getUTCDate() + 3);
  return end.getTime() < today;
};

const observationDate = (weekend: string, lead: number) => {
  const date = new Date(`${weekend}T12:00:00Z`);
  date.setUTCDate(date.getUTCDate() - lead);
  return date.toISOString().slice(0, 10);
};

const sampleLeads = (observed: number[]) => {
  if (observed.length <= MAX_HEATMAP_COLUMNS) return observed;
  const sampled = Array.from({ length: MAX_HEATMAP_COLUMNS }, (_, index) => (
    observed[Math.round((index * (observed.length - 1)) / (MAX_HEATMAP_COLUMNS - 1))]
  ));
  return Array.from(new Set(sampled));
};

export const PriceHeatmap = ({ history, scheduledWeekends = [], selected, onSelect }: PriceHeatmapProps) => {
  const allWeekends = useMemo(
    () => Array.from(new Set([...scheduledWeekends, ...history.cells.map((cell) => cell.weekend)])).sort(),
    [history.cells, scheduledWeekends],
  );
  const today = useMemo(() => {
    const now = new Date();
    return Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate());
  }, []);
  const defaultWeekends = useMemo(() => {
    const past = allWeekends.filter((weekend) => tripHasEnded(weekend, today));
    const upcoming = allWeekends.filter((weekend) => !tripHasEnded(weekend, today));
    return [...past.slice(-DEFAULT_PAST_WEEKENDS), ...upcoming.slice(0, DEFAULT_UPCOMING_WEEKENDS)];
  }, [allWeekends, today]);
  const [showAllWeekends, setShowAllWeekends] = useState(false);
  const weekends = showAllWeekends ? allWeekends : defaultWeekends;
  const leads = useMemo(() => {
    const observed = Array.from(new Set(history.cells.map((cell) => cell.leadDays))).sort((a, b) => b - a);
    return observed.length ? sampleLeads(observed) : fallbackLeads;
  }, [history.cells]);
  const observedLeadCount = useMemo(() => new Set(history.cells.map((cell) => cell.leadDays)).size, [history.cells]);
  const lookup = useMemo(() => new Map(history.cells.map((cell) => [`${cell.weekend}:${cell.leadDays}`, cell])), [history.cells]);
  const [activeIndex, setActiveIndex] = useState(0);
  const [tablePage, setTablePage] = useState(0);
  const cellRefs = useRef<Array<HTMLButtonElement | null>>([]);
  const style = { '--heat-columns': leads.length } as CSSProperties;
  const tablePageCount = Math.max(1, Math.ceil(weekends.length / TABLE_PAGE_SIZE));
  const tableWeekends = weekends.slice(tablePage * TABLE_PAGE_SIZE, (tablePage + 1) * TABLE_PAGE_SIZE);

  useEffect(() => {
    setActiveIndex(0);
    setTablePage(0);
  }, [showAllWeekends, leads.length]);

  useEffect(() => {
    if (tablePage >= tablePageCount) setTablePage(tablePageCount - 1);
  }, [tablePage, tablePageCount]);

  const move = (event: KeyboardEvent<HTMLButtonElement>, row: number, column: number) => {
    let nextRow = row;
    let nextColumn = column;
    if (event.key === 'ArrowRight') nextColumn = Math.min(leads.length - 1, column + 1);
    else if (event.key === 'ArrowLeft') nextColumn = Math.max(0, column - 1);
    else if (event.key === 'ArrowDown') nextRow = Math.min(weekends.length - 1, row + 1);
    else if (event.key === 'ArrowUp') nextRow = Math.max(0, row - 1);
    else if (event.key === 'Home') nextColumn = 0;
    else if (event.key === 'End') nextColumn = leads.length - 1;
    else return;
    event.preventDefault();
    const index = nextRow * leads.length + nextColumn;
    setActiveIndex(index);
    cellRefs.current[index]?.focus();
  };

  const groups = [
    { label: 'Past weekends', items: weekends.filter((weekend) => tripHasEnded(weekend, today)) },
    { label: 'Upcoming weekends', items: weekends.filter((weekend) => !tripHasEnded(weekend, today)) },
  ].filter((group) => group.items.length);

  if (!allWeekends.length) {
    return <div className="empty-state"><strong>No price history yet.</strong><span>The heatmap starts filling after the first completed batch.</span></div>;
  }

  return (
    <div className="heatmap-wrap">
      <div className="heatmap-toolbar">
        <p aria-live="polite">
          Showing {weekends.length} of {allWeekends.length} trip weekends
          {!showAllWeekends && allWeekends.length > defaultWeekends.length ? ' — up to 26 past and 26 upcoming' : ''}.
          {observedLeadCount > leads.length ? ` ${leads.length} representative lead times are shown from ${observedLeadCount} observed dates.` : ''}
        </p>
        {allWeekends.length > defaultWeekends.length && (
          <button type="button" className="text-button" onClick={() => setShowAllWeekends((current) => !current)}>
            {showAllWeekends ? 'Show recent weekends' : `Show all ${allWeekends.length} weekends`}
          </button>
        )}
      </div>
      <div className="heatmap-scroll" role="region" aria-label="Historical price heatmap" tabIndex={0}>
        <div className="heatmap-header" style={style}>
          <span className="sticky-label">Trip weekend</span>
          {leads.map((lead) => <span key={lead}>{lead}<small>days</small></span>)}
        </div>
        {groups.map((group) => (
          <div className="heatmap-period" key={group.label}>
            <div className="heatmap-period-label">{group.label}</div>
            {group.items.map((weekend) => {
              const row = weekends.indexOf(weekend);
              return (
                <div className="heatmap-row" style={style} key={weekend}>
                  <span className="weekend-label">{formatDate(`${weekend}T12:00:00Z`, { day: 'numeric', month: 'short' })}</span>
                  {leads.map((lead, column) => {
                    const cell = lookup.get(`${weekend}:${lead}`);
                    const index = row * leads.length + column;
                    if (!cell) {
                      const expectedOn = observationDate(weekend, lead);
                      const notDue = Date.parse(`${expectedOn}T00:00:00Z`) > today;
                      const label = notDue
                        ? `${formatDate(`${weekend}T12:00:00Z`)}, ${lead} days before departure: observation date ${formatDate(`${expectedOn}T12:00:00Z`)} has not arrived yet`
                        : `${formatDate(`${weekend}T12:00:00Z`)}, ${lead} days before departure: expected crawl observation is missing for ${formatDate(`${expectedOn}T12:00:00Z`)}`;
                      return (
                        <button
                          type="button"
                          className={joinClassNames('heat-cell', 'heat-unobserved', notDue ? 'heat-not-due' : 'heat-missing')}
                          key={lead}
                          ref={(element) => { cellRefs.current[index] = element; }}
                          tabIndex={index === activeIndex ? 0 : -1}
                          aria-disabled="true"
                          aria-label={label}
                          title={label}
                          onFocus={() => setActiveIndex(index)}
                          onKeyDown={(event) => move(event, row, column)}
                        >
                          ·
                        </button>
                      );
                    }
                    const isSelected = selected?.weekend === weekend && selected.leadDays === lead;
                    const retainedFare = cell.observedAt
                      ? `, retained fare observed ${formatDateTime(cell.observedAt)}`
                      : '';
                    const latestCheck = cell.currentObservedAt
                      ? `, latest check ${formatDateTime(cell.currentObservedAt)}: ${availabilityLabel[cell.availability]}${cell.currentDiagnostic ? ` (${cell.currentDiagnostic})` : ''}`
                      : '';
                    const label = `${formatDate(`${weekend}T12:00:00Z`)}, batch date ${formatDate(`${cell.observedOn}T12:00:00Z`)}, ${lead} days before departure: ${cell.priceCents === null ? availabilityLabel[cell.availability] : formatCurrency(cell.priceCents)}${retainedFare}${latestCheck}${cell.coverage === 'partial' ? ', partial coverage' : ''}${cell.freshness === 'stale' ? ', stale fare' : ''}, ${Math.round(cell.sourceScopeRatio * 100)}% requested-source scope`;
                    return (
                      <button
                        type="button"
                        key={lead}
                        ref={(element) => { cellRefs.current[index] = element; }}
                        tabIndex={index === activeIndex ? 0 : -1}
                        className={joinClassNames('heat-cell', cellTone(cell, history.scale.p10Cents, history.scale.p90Cents), isSelected && 'is-selected', cell.coverage === 'partial' && 'is-partial', cell.freshness === 'stale' && 'is-stale', cell.priceCents !== null && cell.availability === 'unknown_due_failure' && 'has-newer-failure')}
                        onFocus={() => setActiveIndex(index)}
                        onKeyDown={(event) => move(event, row, column)}
                        onClick={() => onSelect(cell)}
                        aria-label={label}
                        aria-pressed={isSelected}
                        title={label}
                      >
                        {cellText(cell)}
                      </button>
                    );
                  })}
                </div>
              );
            })}
          </div>
        ))}
      </div>
      <div className="heatmap-legend" aria-label="Heatmap legend">
        <span>Lower fare</span>
        {[1, 2, 3, 4, 5].map((level) => <i className={`heat-${level}`} key={level} />)}
        <span>Higher fare</span>
        <span className="heatmap-range">Scale: {formatCurrency(history.scale.p10Cents)}–{formatCurrency(history.scale.p90Cents)}</span>
        <span><i className="heat-not-on-sale" /> not on sale</span>
        <span><i className="heat-sold-out" /> sold out</span>
        <span><i className="heat-no-option" /> no eligible trip</span>
        <span><i className="heat-failed" /> failed</span>
        <span><i className="heat-not-due" /> observation date not due</span>
        <span><i className="heat-missing" /> expected crawl missing</span>
      </div>
      <p className="keyboard-hint">Use arrow keys to move through cells. Select a cell to inspect what Crowl observed that day.</p>
      <details className="table-alternative">
        <summary>Open accessible price table</summary>
        {tablePageCount > 1 && (
          <div className="table-pagination" aria-label="Price table pages">
            <button type="button" onClick={() => setTablePage((page) => Math.max(0, page - 1))} disabled={tablePage === 0}>Previous table page</button>
            <span aria-live="polite">Page {tablePage + 1} of {tablePageCount}</span>
            <button type="button" onClick={() => setTablePage((page) => Math.min(tablePageCount - 1, page + 1))} disabled={tablePage === tablePageCount - 1}>Next table page</button>
          </div>
        )}
        <div className="table-scroll">
          <table>
            <caption>Observed weekend prices by lead time, rows {tablePage * TABLE_PAGE_SIZE + 1}–{Math.min((tablePage + 1) * TABLE_PAGE_SIZE, weekends.length)} of {weekends.length}</caption>
            <thead><tr><th scope="col">Weekend</th>{leads.map((lead) => <th scope="col" key={lead}>{lead} days</th>)}</tr></thead>
            <tbody>
              {tableWeekends.map((weekend) => (
                <tr key={weekend}>
                  <th scope="row">{formatDate(`${weekend}T12:00:00Z`)}</th>
                  {leads.map((lead) => {
                    const cell = lookup.get(`${weekend}:${lead}`);
                    const expectedOn = observationDate(weekend, lead);
                    const notDue = Date.parse(`${expectedOn}T00:00:00Z`) > today;
                    const emptyLabel = notDue
                      ? `Not due until ${formatDate(`${expectedOn}T12:00:00Z`)}`
                      : `Missing crawl on ${formatDate(`${expectedOn}T12:00:00Z`)}`;
                    return <td key={lead}>{cell ? cell.priceCents === null ? availabilityLabel[cell.availability] : `${formatCurrency(cell.priceCents)}${cell.availability !== 'available' ? ` · latest: ${availabilityLabel[cell.availability]}` : ''}` : emptyLabel}</td>;
                  })}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </details>
    </div>
  );
};
