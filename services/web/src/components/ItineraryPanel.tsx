import { Fragment } from 'react';
import type { HeatmapCell, Journey, JourneyEmptyState } from '../types';
import { availabilityLabel, formatCurrency, formatDateTime, formatDuration, modeLabel } from '../utils';
import { AvailabilityPill, CoveragePill, FreshnessPill, StatusPill } from './StatusPill';

interface ItineraryPanelProps {
  journey: Journey | null;
  emptyState?: JourneyEmptyState | null;
  historicalCell?: HeatmapCell | null;
  onBackToLatest?: () => void;
  synthetic?: boolean;
}

const emptyStateCopy: Record<JourneyEmptyState['availability'], { title: string; detail: string }> = {
  available: {
    title: 'Journey details unavailable',
    detail: 'Crowl received an available result, but its complete itinerary could not be displayed.',
  },
  no_valid_option: {
    title: 'No eligible journey found',
    detail: 'No complete round trip satisfied the Friday 19:00 to Monday 07:00 window, 12-hour direction limit, and minimum 12-hour stay.',
  },
  not_on_sale: {
    title: 'Tickets are not on sale yet',
    detail: 'The requested weekend is outside the booking window reported by the searched providers. Crowl will check it again in the next daily batch.',
  },
  sold_out: {
    title: 'No seats are currently available',
    detail: 'The searched providers reported this weekend as sold out. Availability may change in a later daily batch.',
  },
  unknown_due_failure: {
    title: 'Journey status could not be verified',
    detail: 'The latest collection did not produce a trustworthy answer for this city and weekend. This is not the same as no eligible journey.',
  },
};

export const ItineraryPanel = ({ journey, emptyState, historicalCell, onBackToLatest, synthetic = false }: ItineraryPanelProps) => {
  if (!journey) {
    const copy = emptyState ? emptyStateCopy[emptyState.availability] : emptyStateCopy.no_valid_option;
    return (
      <aside className="itinerary-panel empty-itinerary">
        {historicalCell && (
          <div className="historical-banner">
            <div><strong>Historical observation</strong><span>Batch date {formatDateTime(`${historicalCell.observedOn}T12:00:00Z`)} · no fare revision recorded</span></div>
            {onBackToLatest && <button type="button" className="small-button" onClick={onBackToLatest}>Back to latest</button>}
          </div>
        )}
        <p className="eyebrow">Selected weekend</p>
        <h2>{copy.title}</h2>
        <p>{copy.detail}</p>
        {emptyState && (
          <>
            <div className="pill-row empty-itinerary-state" aria-label="Collection result state">
              <AvailabilityPill value={emptyState.availability} />
              <CoveragePill value={emptyState.coverage} />
              <FreshnessPill value={emptyState.freshness} />
            </div>
            {emptyState.observedAt && <p className="observed-line">Retained fare observed {formatDateTime(emptyState.observedAt)}</p>}
            {emptyState.currentObservedAt && (
              <p className="current-state-line">
                Latest check {formatDateTime(emptyState.currentObservedAt)}: {availabilityLabel[emptyState.availability]}
                {emptyState.currentCrawlRunId && ` · batch ${emptyState.currentCrawlRunId}`}
                {emptyState.currentDiagnostic && ` · ${emptyState.currentDiagnostic}`}
              </p>
            )}
          </>
        )}
      </aside>
    );
  }

  const hasUnprotected = journey.legs.some((leg) => leg.protection === 'unprotected_transfer') || journey.bookings.length > 1;
  const outbound = journey.legs.filter((leg) => leg.direction === 'outbound').sort((a, b) => a.sequence - b.sequence);
  const returning = journey.legs.filter((leg) => leg.direction === 'return').sort((a, b) => a.sequence - b.sequence);
  const isHistorical = Boolean(historicalCell || !journey.live);
  const currentAvailability = historicalCell?.availability ?? journey.availability;
  const currentCoverage = historicalCell?.coverage ?? journey.coverage;
  const currentObservedAt = historicalCell?.currentObservedAt ?? journey.currentObservedAt;
  const currentCrawlRunId = historicalCell?.currentCrawlRunId ?? journey.currentCrawlRunId;
  const currentDiagnostic = historicalCell?.currentDiagnostic ?? journey.currentDiagnostic;

  return (
    <aside className="itinerary-panel" aria-labelledby="itinerary-title">
      {historicalCell && (
        <div className="historical-banner">
          <div><strong>Historical observation</strong><span>Seen on {formatDateTime(`${historicalCell.observedOn}T12:00:00Z`)} · not a live quote</span></div>
          {onBackToLatest && <button type="button" className="small-button" onClick={onBackToLatest}>Back to latest</button>}
        </div>
      )}
      <div className="itinerary-price-row">
        <div>
          <p className="eyebrow">Best observed round trip</p>
          <h2 id="itinerary-title">{formatCurrency(journey.totalPriceCents, journey.currency)}</h2>
          <p>{modeLabel(journey.mode)} · one traveler</p>
        </div>
        <div className="pill-stack">
          {synthetic && <StatusPill label="Synthetic data" tone="warning" />}
          <AvailabilityPill value={currentAvailability} />
          <CoveragePill value={currentCoverage} />
          <FreshnessPill value={journey.freshness} />
        </div>
      </div>
      <p className="observed-line">Retained fare observed {formatDateTime(journey.observedAt)}</p>
      {currentObservedAt && (
        <p className="current-state-line">
          Latest check {formatDateTime(currentObservedAt)}: {availabilityLabel[currentAvailability]}
          {currentCrawlRunId && ` · batch ${currentCrawlRunId}`}
          {currentDiagnostic && ` · ${currentDiagnostic}`}
        </p>
      )}

      {hasUnprotected && (
        <div className="connection-warning" role="note">
          <span aria-hidden="true">!</span>
          <div><strong>Separate or unprotected tickets</strong><p>A missed connection may not be covered. Recheck every component price before booking.</p></div>
        </div>
      )}

      <Direction title="Outbound" legs={outbound} />
      <Direction title="Return" legs={returning} />

      <section className="booking-components" aria-labelledby="booking-title">
        <div className="subsection-heading"><h3 id="booking-title">Tickets to purchase</h3><span>{journey.bookings.length} {journey.bookings.length === 1 ? 'booking' : 'bookings'}</span></div>
        {journey.bookings.map((booking) => {
          const expiresAt = booking.expiresAt ? Date.parse(booking.expiresAt) : Number.NaN;
          const isExpired = Number.isFinite(expiresAt) && expiresAt <= Date.now();
          const canOpenBooking = Boolean(
            booking.bookingUrl
            && !isHistorical
            && currentAvailability === 'available'
            && journey.freshness === 'fresh'
            && booking.bookableAtObservation !== false
            && !isExpired,
          );
          const unavailableReason = isHistorical
            ? 'Historical only'
            : currentAvailability !== 'available'
              ? 'Latest availability unverified'
              : journey.freshness !== 'fresh'
                ? 'Stale observation'
                : isExpired
                  ? 'Offer link expired'
                  : booking.bookableAtObservation === false
                    ? 'Not bookable at observation'
                    : 'Link unavailable';
          return (
            <div className="booking-row" key={booking.id}>
              <div>
                <strong>{booking.seller}</strong>
                <span>
                  {booking.source} · {booking.fareName ?? 'Fare name unavailable'}
                  {booking.studentFare ? ' · student/youth fare' : ''}
                  {booking.mandatoryFeesIncluded ? ' · mandatory fees included' : ''}
                  {booking.passengerEligibilityConfirmed && booking.passengerAge
                    ? ` · age ${booking.passengerAge} eligibility verified`
                    : ''}
                </span>
              </div>
              <strong>{formatCurrency(booking.priceCents, booking.currency)}</strong>
              {canOpenBooking
                ? <a className="outline-button" href={booking.bookingUrl ?? undefined} target="_blank" rel="noreferrer">Check with seller</a>
                : <span className="booking-link-unavailable">{unavailableReason}</span>}
            </div>
          );
        })}
      </section>
      <p className="fine-print">Times are shown in the terminal’s local time. Weekend eligibility is evaluated in Europe/Paris. Fare and availability can change at any time.</p>
    </aside>
  );
};

const Direction = ({ title, legs }: { title: string; legs: Journey['legs'] }) => {
  const elapsedMinutes = legs.length
    ? Math.max(0, Math.round((new Date(legs[legs.length - 1].arrivesAt).getTime() - new Date(legs[0].departsAt).getTime()) / 60_000))
    : null;
  return (
    <section className="direction-block" aria-label={`${title} journey`}>
      <div className="subsection-heading">
        <h3>{title}</h3>
        <span>{formatDuration(elapsedMinutes)}</span>
      </div>
      {legs.length ? legs.map((leg, index) => {
        const next = legs[index + 1];
        const connectionMinutes = next
          ? Math.max(0, Math.round((new Date(next.departsAt).getTime() - new Date(leg.arrivesAt).getTime()) / 60_000))
          : null;
        return (
          <Fragment key={leg.id}>
            <div className="journey-leg">
              <div className="leg-rail" aria-hidden="true"><i />{index < legs.length - 1 && <span />}</div>
              <div className="leg-main">
                <div className="leg-times"><strong>{formatDateTime(leg.departsAt, leg.originTimezone)}</strong><span>{formatDuration(leg.durationMinutes)}</span><strong>{formatDateTime(leg.arrivesAt, leg.destinationTimezone)}</strong></div>
                <div className="leg-terminals"><strong>{leg.originTerminal}</strong><span aria-hidden="true">→</span><strong>{leg.destinationTerminal}</strong></div>
                <div className="leg-meta"><span>{modeLabel(leg.mode)}</span><span>{leg.operator}</span>{leg.serviceNumber && <span>{leg.serviceNumber}</span>}{leg.protection === 'unprotected_transfer' && <StatusPill label="Unprotected" tone="warning" />}</div>
              </div>
            </div>
            {next && <div className="connection-wait" role="note">Connection at {leg.destinationTerminal}: {formatDuration(connectionMinutes)}</div>}
          </Fragment>
        );
      }) : <p className="muted-text">Detailed legs were not provided by this source.</p>}
    </section>
  );
};
