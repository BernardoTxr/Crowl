import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { CrowlRoutes } from '../App';
import { api } from '../api/client';
import type { City, Journey } from '../types';

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
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new TypeError('API unavailable')));
  });
  afterEach(() => vi.restoreAllMocks());

  it('renders the Schengen explorer with mock fallback and interactive filters', async () => {
    const user = userEvent.setup();
    renderRoute('/');

    expect(await screen.findByRole('heading', { name: /find a weekend worth/i })).toBeInTheDocument();
    expect(await screen.findByText(/mock city catalog/i)).toBeInTheDocument();
    expect((await screen.findAllByText('Amsterdam')).length).toBeGreaterThan(0);

    const weekendSelect = screen.getByLabelText('Weekend');
    await user.selectOptions(weekendSelect, within(weekendSelect).getAllByRole('option')[1]);
    expect(await screen.findByText('Lowest observed')).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: /train/i }));
    expect(screen.getByRole('button', { name: /train/i })).toHaveAttribute('aria-pressed', 'true');
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
    vi.spyOn(api, 'getCity').mockResolvedValue({ data: city, isMock: false });
    vi.spyOn(api, 'getWeekends').mockResolvedValue({ data: [{ friday: '2027-01-08', startsAt: '2027-01-08T19:00:00Z', endsAt: '2027-01-11T06:00:00Z', label: '8 Jan', minimumStayHours: 12, maxDirectionHours: 12 }], isMock: false });
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
      isMock: false,
    });
    const journeySpy = vi.spyOn(api, 'getJourney').mockResolvedValue({ data: journey, isMock: false });

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
      isMock: false,
    });
    vi.spyOn(api, 'getExtractors').mockResolvedValue({ data: [], isMock: false });

    renderRoute('/status');

    expect(await screen.findByRole('heading', { name: 'Stale batch' })).toBeInTheDocument();
    expect(screen.getByText('Stale · degraded')).toBeInTheDocument();
    expect(screen.getByText('No applicable enabled work')).toBeInTheDocument();
    expect(screen.queryByRole('progressbar', { name: 'Execution coverage' })).not.toBeInTheDocument();
    expect(screen.queryByText('0%')).not.toBeInTheDocument();
  });
});
