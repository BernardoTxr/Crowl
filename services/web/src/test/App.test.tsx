import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { CrowlRoutes } from '../App';
import { api } from '../api/client';
import type { Batch, CatalogMeta, City, Extractor, Journey, MapFare, PriceHistory, Weekend } from '../types';

const catalogMeta: CatalogMeta = {
  catalogVariant: 'final',
  categoryDefinitions: [{ id: 'culture', label: 'Culture' }, { id: 'food', label: 'Food' }],
  attributeDefinitions: [{ id: 'expense', label: 'Expensive', min: 1, max: 5, lowLabel: 'Affordable', highLabel: 'Very expensive' }],
};

const city = (slug: string, name: string, code: string, country: string, lat: number, lon: number, visited = false): City => ({
  id: `${slug}-${code.toLowerCase()}`,
  slug,
  name,
  country: { code, name: country },
  timezone: code === 'AT' ? 'Europe/Vienna' : code === 'IT' ? 'Europe/Rome' : 'Europe/Amsterdam',
  coordinates: { lat, lon },
  status: { visited, wishlist: !visited },
  categories: ['culture'],
  summary: `${name} catalog summary`,
  attributes: { expense: 3 },
  photo: { asset: '/crowl-mark.svg', alt: name },
  typicalDurations: [{ mode: 'flight', medianMinutes: 120, p25Minutes: 105, p75Minutes: 140, sampleCount: 20 }],
});

const cities: City[] = [
  city('amsterdam', 'Amsterdam', 'NL', 'Netherlands', 52.3676, 4.9041, true),
  city('rome', 'Rome', 'IT', 'Italy', 41.9028, 12.4964, true),
  city('vienna', 'Vienna', 'AT', 'Austria', 48.2082, 16.3738),
];

const weekend: Weekend = {
  friday: '2027-01-08',
  startsAt: '2027-01-08T19:00:00+01:00',
  endsAt: '2027-01-11T07:00:00+01:00',
  label: '8 Jan — 11 Jan',
  minimumStayHours: 12,
  maxDirectionHours: 12,
};

const fares: MapFare[] = [{
  citySlug: 'amsterdam',
  priceCents: 8_900,
  winningMode: 'train',
  availability: 'available',
  coverage: 'complete',
  sourceScopeRatio: 1,
  freshness: 'fresh',
  observedAt: '2026-12-01T02:00:00Z',
  currentObservedAt: '2026-12-01T02:00:00Z',
  currentCrawlRunId: '10',
  currentDiagnostic: null,
  originTerminal: 'Paris Gare du Nord',
  destinationTerminal: 'Amsterdam Centraal',
}];

const priceHistory: PriceHistory = {
  cells: [{
    weekend: weekend.friday,
    observedOn: '2026-12-01',
    leadDays: 38,
    priceCents: 12_300,
    availability: 'available',
    coverage: 'complete',
    sourceScopeRatio: 1,
    freshness: 'fresh',
    observedAt: '2026-12-01T02:00:00Z',
    currentObservedAt: '2026-12-01T02:00:00Z',
    currentCrawlRunId: '10',
    currentDiagnostic: null,
    revisionId: '41',
  }],
  scale: { p10Cents: 12_300, p90Cents: 12_300 },
};

const journey: Journey = {
  id: '41', totalPriceCents: 12_300, currency: 'EUR', mode: 'flight',
  observedAt: '2026-12-01T02:00:00Z', currentObservedAt: '2026-12-01T02:00:00Z',
  currentCrawlRunId: '10', currentDiagnostic: null,
  outboundDepartureAt: '2027-01-08T20:00:00+01:00', returnArrivalAt: '2027-01-10T23:00:00+01:00',
  coverage: 'complete', sourceScopeRatio: 1, freshness: 'fresh', availability: 'available', live: true,
  legs: [
    { id: 'out', direction: 'outbound', sequence: 1, mode: 'flight', operator: 'Carrier', originTerminal: 'Paris Orly', destinationTerminal: 'Vienna International Airport', departsAt: '2027-01-08T20:00:00+01:00', arrivesAt: '2027-01-08T22:00:00+01:00', durationMinutes: 120, protection: 'protected' },
    { id: 'return', direction: 'return', sequence: 1, mode: 'flight', operator: 'Carrier', originTerminal: 'Vienna International Airport', destinationTerminal: 'Paris Orly', departsAt: '2027-01-10T21:00:00+01:00', arrivesAt: '2027-01-10T23:00:00+01:00', durationMinutes: 120, protection: 'protected' },
  ],
  bookings: [],
};

const batch: Batch = {
  id: 'batch-1', scheduledFor: '2026-11-20', status: 'succeeded', state: 'current',
  startedAt: '2026-11-20T02:00:00Z', heartbeatAt: '2026-11-20T02:05:00Z',
  completedAt: '2026-11-20T02:10:00Z', publishedAt: '2026-11-20T02:11:00Z',
  expectedCells: 140, completedCells: 140, enabledSources: 14, knownSources: 14,
  latestTrustworthyObservationAt: '2026-11-20T02:08:00Z', nextExpectedAt: '2026-11-21T02:00:00Z', diagnostic: null,
};

const extractorDefinitions: Array<[string, string, 'flight' | 'train' | 'bus']> = [
  ['ryanair', 'Ryanair', 'flight'], ['easyjet', 'easyJet', 'flight'], ['transavia', 'Transavia', 'flight'],
  ['wizzair', 'Wizz Air', 'flight'], ['vueling', 'Vueling', 'flight'], ['volotea', 'Volotea', 'flight'],
  ['flixbus', 'FlixBus', 'bus'], ['blablacar-bus', 'BlaBlaCar Bus', 'bus'], ['itabus', 'Itabus', 'bus'],
  ['alsa', 'Alsa', 'bus'], ['ouigo', 'OUIGO', 'train'], ['sncf-connect', 'SNCF Connect', 'train'],
  ['trenitalia-france', 'Trenitalia France', 'train'], ['eurostar', 'Eurostar', 'train'],
];

const extractors: Extractor[] = extractorDefinitions.map(([source, displayName, mode]) => ({
  source, displayName, modes: [mode], strategy: 'http_structured', version: '1.0.0', enabled: true,
  disabledReason: null, state: 'succeeded', lastAttemptAt: '2026-11-20T02:00:00Z',
  lastSuccessAt: '2026-11-20T02:00:00Z', runtimeSeconds: 60, consecutiveFailures: 0,
  counts: { expected: 10, attempted: 10, successful: 10, empty: 2, withResults: 8, physicalRequests: 10 },
  coverageRatio: 1, sourceScopeRatio: 1, freshness: 'fresh', diagnostic: null,
}));

const renderRoute = (route: string) => {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[route]}>
        <CrowlRoutes />
      </MemoryRouter>
    </QueryClientProvider>,
  );
};

describe('Crowl application', () => {
  beforeEach(() => {
    vi.spyOn(api, 'getMeta').mockResolvedValue({ data: catalogMeta });
    vi.spyOn(api, 'getWeekends').mockResolvedValue({ data: [weekend] });
    vi.spyOn(api, 'getCities').mockResolvedValue({ data: cities });
    vi.spyOn(api, 'getCity').mockImplementation(async (slug) => ({ data: cities.find((item) => item.slug === slug) ?? null }));
    vi.spyOn(api, 'getMapFares').mockResolvedValue({ data: fares });
    vi.spyOn(api, 'getPriceHistory').mockResolvedValue({ data: priceHistory });
    vi.spyOn(api, 'getJourney').mockResolvedValue({ data: journey });
    vi.spyOn(api, 'getLatestBatch').mockResolvedValue({ data: batch });
    vi.spyOn(api, 'getExtractors').mockResolvedValue({ data: extractors });
  });
  afterEach(() => vi.restoreAllMocks());

  it('renders the Schengen explorer with API catalog data and interactive filters', async () => {
    const user = userEvent.setup();
    renderRoute('/');

    expect(await screen.findByRole('heading', { name: 'Schengen travel map' })).toBeInTheDocument();
    expect(screen.getByRole('region', { name: 'Interactive geographic map of Europe' })).toBeInTheDocument();
    expect(screen.queryByText(/find a weekend worth/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/looking ahead/i)).not.toBeInTheDocument();
    expect(screen.queryByText('Saved memory')).not.toBeInTheDocument();
    expect(screen.queryByText('Still to discover')).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Mixed' })).not.toBeInTheDocument();
    expect((await screen.findAllByText('Amsterdam')).length).toBeGreaterThan(0);
    expect(document.querySelector('.city-card-price')).not.toBeInTheDocument();

    const weekendSelect = screen.getByLabelText('Weekend');
    await user.selectOptions(weekendSelect, within(weekendSelect).getAllByRole('option')[1]);
    expect(await screen.findByText('Round trip')).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: /train/i }));
    expect(screen.getByRole('button', { name: /train/i })).toHaveAttribute('aria-pressed', 'true');
  });

  it('treats the removed mixed filter as Any', async () => {
    renderRoute('/?mode=mixed');

    const anyMode = await screen.findByRole('button', { name: 'Any' });
    expect(anyMode).toHaveAttribute('aria-pressed', 'true');
    expect(screen.queryByRole('button', { name: 'Mixed' })).not.toBeInTheDocument();
  });

  it('shows a dual-status city as visited only', async () => {
    renderRoute('/cities/rome');

    expect(await screen.findByRole('heading', { level: 1, name: 'Rome' })).toBeInTheDocument();
    expect(screen.getByText('✓ Visited')).toBeInTheDocument();
    expect(screen.queryByText('☆ Wishlist')).not.toBeInTheDocument();
  });

  it('renders a city profile, heatmap, and winning itinerary', async () => {
    renderRoute('/cities/vienna');

    expect(await screen.findByRole('heading', { level: 1, name: 'Vienna' })).toBeInTheDocument();
    expect(await screen.findByRole('heading', { name: 'Price history' })).toBeInTheDocument();
    expect(await screen.findByText(/best observed round trip/i)).toBeInTheDocument();
    expect(screen.getAllByText(/Vienna International Airport/)).toHaveLength(2);
    expect(screen.getByRole('region', { name: /historical price heatmap/i })).toBeInTheDocument();
  });

  it('does not fetch the latest journey for a selected no-revision history cell', async () => {
    const user = userEvent.setup();
    const city: City = {
      id: 'vienna-at', slug: 'vienna', name: 'Vienna',
      country: { code: 'AT', name: 'Austria' }, timezone: 'Europe/Vienna',
      coordinates: { lat: 48.2, lon: 16.37 }, status: { visited: false, wishlist: true },
      categories: ['culture'], summary: 'Vienna summary', attributes: { expense: 4 },
      photo: { asset: '/crowl-mark.svg', alt: 'Vienna' },
    };
    const journey: Journey = {
      id: 'latest-1', totalPriceCents: 8_900, currency: 'EUR', mode: 'flight',
      observedAt: '2026-12-01T02:00:00Z', currentObservedAt: '2026-12-01T03:00:00Z',
      currentCrawlRunId: '10', currentDiagnostic: null,
      outboundDepartureAt: '2027-01-08T20:00:00Z', returnArrivalAt: '2027-01-10T22:00:00Z',
      coverage: 'complete', sourceScopeRatio: 1, freshness: 'fresh', availability: 'available',
      live: true, legs: [], bookings: [],
    };
    vi.spyOn(api, 'getCity').mockResolvedValue({ data: city });
    vi.spyOn(api, 'getWeekends').mockResolvedValue({ data: [{ friday: '2027-01-08', startsAt: '2027-01-08T19:00:00Z', endsAt: '2027-01-11T06:00:00Z', label: '8 Jan', minimumStayHours: 12, maxDirectionHours: 12 }] });
    vi.spyOn(api, 'getPriceHistory').mockResolvedValue({
      data: {
        cells: [{
          weekend: '2027-01-08', observedOn: '2027-01-01', leadDays: 7,
          priceCents: null, availability: 'unknown_due_failure', coverage: 'failed',
          sourceScopeRatio: 1, freshness: 'never_observed', observedAt: null,
          currentObservedAt: '2027-01-01T03:00:00Z', currentCrawlRunId: '11',
          currentDiagnostic: 'UPSTREAM_FAILURE', revisionId: null,
        }],
        scale: { p10Cents: 5_000, p90Cents: 25_000 },
      },
    });
    const journeySpy = vi.spyOn(api, 'getJourney').mockResolvedValue({ data: journey });

    renderRoute('/cities/vienna?weekend=2027-01-08');
    expect(await screen.findByText(/Best observed round trip/i)).toBeInTheDocument();
    expect(journeySpy).toHaveBeenCalledTimes(1);

    await user.click(screen.getByRole('button', { name: /batch date .*Collection failed/i }));

    expect(await screen.findByRole('heading', { name: 'Journey status could not be verified' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Back to latest' })).toBeInTheDocument();
    await waitFor(() => expect(journeySpy).toHaveBeenCalledTimes(1));
  });

  it('shows all requested source connectors in the read-only status view', async () => {
    renderRoute('/status');

    expect(await screen.findByRole('heading', { level: 1, name: 'Source health' })).toBeInTheDocument();
    expect(await screen.findByText('Ryanair', { selector: 'strong' })).toBeInTheDocument();
    expect(screen.getByText('Eurostar', { selector: 'strong' })).toBeInTheDocument();
    expect(screen.getByText(/showing 14 sources/i)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /retry/i })).not.toBeInTheDocument();
  });

  it('opens a source from a shareable status query without adding mutation controls', async () => {
    renderRoute('/status?source=sncf-connect');

    expect(await screen.findByText('SNCF Connect', { selector: 'strong' })).toBeInTheDocument();
    const sourceFilter = screen.getByLabelText('Filter by source');
    expect(sourceFilter).toHaveValue('sncf-connect');
    expect(screen.getByText(/showing 1 sources/i)).toBeInTheDocument();
    expect(document.querySelectorAll('.extractor-card')).toHaveLength(1);
    expect(document.querySelector('.extractor-card')).toHaveAttribute('open');
    expect(screen.queryByRole('button', { name: /retry|enable|disable/i })).not.toBeInTheDocument();
  });

  it('labels stale successful batches as degraded and zero expected work as not applicable', async () => {
    vi.spyOn(api, 'getLatestBatch').mockResolvedValue({
      data: {
        id: 'batch-1', scheduledFor: '2026-11-20', status: 'succeeded', state: 'stale',
        startedAt: '2026-11-20T02:00:00Z', heartbeatAt: '2026-11-20T02:05:00Z',
        completedAt: '2026-11-20T02:10:00Z', publishedAt: '2026-11-20T02:11:00Z',
        expectedCells: 0, completedCells: 0, enabledSources: 0, knownSources: 14,
        latestTrustworthyObservationAt: '2026-11-18T02:00:00Z', nextExpectedAt: '2026-11-21T02:00:00Z',
        diagnostic: null,
      },
    });
    vi.spyOn(api, 'getExtractors').mockResolvedValue({ data: [] });

    renderRoute('/status');

    expect(await screen.findByRole('heading', { name: 'Stale batch' })).toBeInTheDocument();
    expect(screen.getByText('Stale · degraded')).toBeInTheDocument();
    expect(screen.getByText('No applicable enabled work')).toBeInTheDocument();
    expect(screen.queryByRole('progressbar', { name: 'Execution coverage' })).not.toBeInTheDocument();
    expect(screen.queryByText('0%')).not.toBeInTheDocument();
  });
});
