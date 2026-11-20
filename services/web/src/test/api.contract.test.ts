import { beforeEach, describe, expect, it, vi } from 'vitest';
import { api } from '../api/client';

const city = {
  id: 'vienna-at', slug: 'vienna', name: 'Vienna',
  country: { code: 'AT', name: 'Austria' }, timezone: 'Europe/Vienna',
  coordinates: { lat: 48.2, lon: 16.37 }, status: { visited: false, wishlist: true },
  categories: ['culture'], attributes: { expense: 4 }, summary: 'Vienna summary',
  photo: null,
};

const response = (payload: unknown) => Promise.resolve(new Response(JSON.stringify(payload), {
  status: 200,
  headers: { 'Content-Type': 'application/json' },
}));

describe('FastAPI contract normalization', () => {
  beforeEach(() => vi.stubGlobal('fetch', vi.fn()));

  it('normalizes nested city details and exact terminal journey fields', async () => {
    const fetchMock = vi.mocked(fetch);
    fetchMock
      .mockImplementationOnce(() => response({ city, typical_durations: [{ mode: 'train', state: 'available', p25_minutes: 620, median_minutes: 650, p75_minutes: 700, sample_count: 18 }] }))
      .mockImplementationOnce(() => response({
        city, weekend: '2027-01-08', mode: 'train', availability: 'available', execution_coverage: 'complete', freshness: 'fresh', current_observed_at: '2026-12-02T03:00:00Z', current_crawl_run_id: 101, current_diagnostic: 'CURRENT_FARE_AVAILABLE',
        items: [{
          revision_id: 99, rule_version_id: 4, profile_id: 2, total_price_eur_cents: 12340, mode: 'train', observed_at: '2026-12-01T02:00:00Z', historical: true,
          availability: 'available', execution_coverage: 'complete', source_scope_ratio: 0.64, freshness: 'fresh', outbound_departure_at: '2027-01-08T19:30:00Z', return_arrival_at: '2027-01-10T21:30:00Z',
          bookings: [{ id: 5, source: 'sncf_connect', seller: 'Rail seller', fare_name: 'Youth', original_currency: 'GBP', eur_amount_cents: 12340, student_fare_used: true, mandatory_fees_included: true, passenger_age: 22, passenger_student: true, passenger_eligibility_confirmed: true, bookable_at_observation: true, is_teaser: false, booking_url: null, expires_at: '2027-01-08T18:00:00Z' }],
          legs: [{ id: 8, direction: 'outbound', segment_order: 1, mode: 'train', operator: 'Night Rail', service_number: 'NR1', origin_terminal: { id: 'par', name: 'Paris Gare de l’Est', kind: 'rail', timezone: 'Europe/Paris' }, destination_terminal: { id: 'vie', name: 'Wien Hauptbahnhof', kind: 'rail', timezone: 'Europe/Vienna' }, departs_at: '2027-01-08T19:30:00Z', arrives_at: '2027-01-09T05:40:00Z', origin_timezone: 'Europe/Paris', destination_timezone: 'Europe/Vienna', protection_state: 'protected' }],
        }],
      }));

    const detail = await api.getCity('vienna');
    const journey = await api.getJourney('vienna', '2027-01-08', 'train');

    expect(detail.isMock).toBe(false);
    expect(detail.data?.typicalDurations?.[0]).toMatchObject({ mode: 'train', medianMinutes: 650, sampleCount: 18 });
    expect(journey.data).toMatchObject({ id: '99', ruleVersionId: 4, profileId: 2, totalPriceCents: 12340, live: false, observedAt: '2026-12-01T02:00:00Z', currentObservedAt: '2026-12-02T03:00:00Z', currentCrawlRunId: '101', currentDiagnostic: 'CURRENT_FARE_AVAILABLE' });
    expect(journey.isSynthetic).toBe(false);
    expect(journey.data?.bookings[0]).toMatchObject({ id: '5', source: 'sncf_connect', currency: 'EUR', studentFare: true, mandatoryFeesIncluded: true, passengerAge: 22, passengerStudent: true, passengerEligibilityConfirmed: true, bookableAtObservation: true, isTeaser: false, expiresAt: '2027-01-08T18:00:00Z' });
    expect(journey.data?.legs[0]).toMatchObject({ id: '8', sequence: 1, originTerminal: 'Paris Gare de l’Est', destinationTimezone: 'Europe/Vienna' });
  });

  it('handles numeric batch IDs, envelope scheduling, extractor metrics, and never-observed fares', async () => {
    const fetchMock = vi.mocked(fetch);
    fetchMock
      .mockImplementationOnce(() => response({ batch: { id: 7, scheduled_for: '2026-11-20', status: 'succeeded', started_at: null, heartbeat_at: null, completed_at: null, published_at: null, expected_cells: 100, completed_cells: 75, latest_trustworthy_observation_at: null }, state: 'stale', next_expected_batch_at: '2026-11-21T02:00:00Z' }))
      .mockImplementationOnce(() => response({ batch_id: 7, items: [{ source: 'sncf_connect', display_name: 'SNCF Connect', modes: ['train'], strategy: 'http_structured', connector_version: '1.2.0', parser_version: '1.1.0', enabled: true, disabled_reason: null, state: 'succeeded', last_attempt_at: '2026-11-20T03:00:00Z', last_success_at: '2026-11-18T03:00:00Z', freshness: 'stale', runtime_ms: 1500, consecutive_failures: 0, execution_coverage_ratio: 0.75, source_scope_ratio: 0.64, counts: { expected: 100, attempted: 80, successful: 75, empty: 5, result_bearing: 70, physical_requests: 40 }, diagnostic: null }] }))
      .mockImplementationOnce(() => response({ weekend: '2027-01-08', mode: 'all', items: [{ city, price_eur_cents: 8900, winning_mode: 'flight', rule_version_id: 4, profile_id: 2, availability: 'unknown_due_failure', execution_coverage: 'failed', source_scope_ratio: 0, is_synthetic: true, freshness: 'stale', observed_at: '2026-11-20T02:00:00Z', current_observed_at: '2026-11-21T03:00:00Z', current_crawl_run_id: 8, current_diagnostic: 'UPSTREAM_FAILURE', origin_terminal: { name: 'Paris Orly' }, destination_terminal: { name: 'Vienna Airport' }, return_origin_terminal: { name: 'Wien Hauptbahnhof' }, return_destination_terminal: { name: 'Paris Gare de l’Est' } }] }))
      .mockImplementationOnce(() => response({ city, mode: 'all', scale: { p10_eur_cents: 8900, p90_eur_cents: 8900 }, cells: [{ weekend: '2027-01-08', observed_on: '2026-11-21', lead_days: 48, price_eur_cents: 8900, availability: 'unknown_due_failure', execution_coverage: 'failed', source_scope_ratio: 1, freshness: 'stale', observed_at: '2026-11-20T02:00:00Z', current_observed_at: '2026-11-21T03:00:00Z', current_crawl_run_id: 8, current_diagnostic: 'UPSTREAM_FAILURE', revision_id: 99, rule_version_id: 4, profile_id: 2, winning_mode: 'flight' }] }));

    const batch = await api.getLatestBatch();
    const extractors = await api.getExtractors();
    const map = await api.getMapFares('2027-01-08', 'all');
    const history = await api.getPriceHistory('vienna', 'all');

    expect(batch.data).toMatchObject({ id: '7', status: 'succeeded', state: 'stale', nextExpectedAt: '2026-11-21T02:00:00Z' });
    expect(extractors.data[0]).toMatchObject({ runtimeSeconds: 1.5, coverageRatio: 0.75, freshness: 'stale', version: '1.2.0 · parser 1.1.0' });
    expect(extractors.data[0].counts.withResults).toBe(70);
    expect(map.data[0]).toMatchObject({ citySlug: 'vienna', availability: 'unknown_due_failure', freshness: 'stale', priceCents: 8900, observedAt: '2026-11-20T02:00:00Z', currentObservedAt: '2026-11-21T03:00:00Z', currentCrawlRunId: '8', currentDiagnostic: 'UPSTREAM_FAILURE', ruleVersionId: 4, profileId: 2, originTerminal: 'Paris Orly', returnDestinationTerminal: 'Paris Gare de l’Est' });
    expect(history.data.cells[0]).toMatchObject({ priceCents: 8900, availability: 'unknown_due_failure', observedAt: '2026-11-20T02:00:00Z', currentObservedAt: '2026-11-21T03:00:00Z', currentCrawlRunId: '8', currentDiagnostic: 'UPSTREAM_FAILURE' });
    expect(map.isSynthetic).toBe(true);
  });

  it('preserves successful empty API states instead of inventing fallback fares', async () => {
    const fetchMock = vi.mocked(fetch);
    fetchMock
      .mockImplementationOnce(() => response({ city, weekend: '2027-01-08', mode: 'all', availability: 'no_valid_option', execution_coverage: 'complete', freshness: 'fresh', observed_at: null, current_observed_at: '2026-11-21T03:00:00Z', current_crawl_run_id: 9, current_diagnostic: 'NO_VALID_OPTION', items: [] }))
      .mockImplementationOnce(() => response({ city, mode: 'all', scale: { p10_eur_cents: null, p90_eur_cents: null }, cells: [] }));

    const journey = await api.getJourney('vienna', '2027-01-08', 'all');
    const history = await api.getPriceHistory('vienna', 'all');

    expect(journey).toMatchObject({
      data: null,
      isMock: false,
      isSynthetic: false,
      emptyState: {
        availability: 'no_valid_option',
        coverage: 'complete',
        freshness: 'fresh',
        observedAt: null,
        currentObservedAt: '2026-11-21T03:00:00Z',
        currentCrawlRunId: '9',
        currentDiagnostic: 'NO_VALID_OPTION',
      },
    });
    expect(history).toMatchObject({ data: { cells: [] }, isMock: false, isSynthetic: false });
  });

  it('requests the exact revision selected from the history heatmap', async () => {
    const fetchMock = vi.mocked(fetch);
    fetchMock.mockImplementationOnce(() => response({
      city,
      weekend: '2027-01-08',
      mode: 'all',
      availability: 'available',
      execution_coverage: 'complete',
      freshness: 'fresh',
      items: [{
        revision_id: 41,
        total_price_eur_cents: 9900,
        mode: 'flight',
        observed_at: '2026-11-01T02:00:00Z',
        historical: true,
        availability: 'available',
        execution_coverage: 'complete',
        source_scope_ratio: 1,
        freshness: 'fresh',
        bookings: [],
        legs: [],
      }],
    }));

    const journey = await api.getJourney('vienna', '2027-01-08', 'all', '41');

    expect(journey.data?.id).toBe('41');
    expect(fetchMock).toHaveBeenCalledWith(
      '/api/v1/cities/vienna/journeys?weekend=2027-01-08&mode=all&revision_id=41',
      expect.any(Object),
    );
  });
});
