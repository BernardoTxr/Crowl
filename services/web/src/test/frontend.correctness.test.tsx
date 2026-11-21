import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { describe, expect, it, vi } from 'vitest';
import { CrowlMap } from '../components/CrowlMap';
import { CityCard } from '../components/CityCard';
import { ItineraryPanel } from '../components/ItineraryPanel';
import { PriceHeatmap } from '../components/PriceHeatmap';
import type { City, Journey, Weekend } from '../types';
import { FLIXBUS_DISPLAYED_FARE_DISCLOSURE, nextNonpastWeekend } from '../utils';

const MAP_TILE_SIZE_FOR_TEST = 512;

describe('frontend interaction correctness', () => {
  const mapCity: City = {
    id: 'vienna-at',
    slug: 'vienna',
    name: 'Vienna',
    country: { code: 'AT', name: 'Austria' },
    timezone: 'Europe/Vienna',
    coordinates: { lat: 48.2, lon: 16.37 },
    status: { visited: false, wishlist: true },
    categories: ['culture'],
    summary: 'Vienna summary',
    attributes: { expense: 4 },
    photo: { asset: '/crowl-mark.svg', alt: 'Vienna' },
  };

  it('separates a retained map fare from a newer failed check', () => {
    render(
      <MemoryRouter>
        <CrowlMap
          cities={[mapCity]}
          fares={[{
            citySlug: 'vienna',
            priceCents: 8_900,
            winningMode: 'flight',
            availability: 'unknown_due_failure',
            coverage: 'failed',
            sourceScopeRatio: 1,
            freshness: 'stale',
            observedAt: '2026-11-01T02:00:00Z',
            currentObservedAt: '2026-11-02T03:00:00Z',
            currentCrawlRunId: '42',
            currentDiagnostic: 'UPSTREAM_FAILURE',
            originTerminal: 'Paris Orly',
            destinationTerminal: 'Vienna Airport',
          }]}
          weekend="2027-01-08"
          mode="all"
        />
      </MemoryRouter>,
    );

    const marker = screen.getByRole('link', {
      name: /Vienna, €89, latest check Collection failed\. View city\./,
    });
    fireEvent.mouseEnter(marker);

    expect(screen.getByText(/Retained fare observed/)).toBeInTheDocument();
    expect(screen.getByText(/Latest check .*Collection failed.*batch 42.*UPSTREAM_FAILURE/)).toBeInTheDocument();
  });

  it('labels FlixBus displayed-card prices wherever a winner is presented', () => {
    const displayedFare = {
      citySlug: 'vienna',
      priceCents: 5_500,
      winningMode: 'bus' as const,
      availability: 'available' as const,
      coverage: 'complete' as const,
      sourceScopeRatio: 1,
      freshness: 'fresh' as const,
      observedAt: '2026-11-01T02:00:00Z',
      currentObservedAt: '2026-11-01T02:00:00Z',
      currentCrawlRunId: '42',
      currentDiagnostic: null,
      originTerminal: 'Paris Bercy Seine',
      destinationTerminal: 'Vienna Erdberg',
      priceCompleteness: 'estimated' as const,
      mandatoryFeesIncluded: false,
      fareDisclosure: FLIXBUS_DISPLAYED_FARE_DISCLOSURE,
    };
    const card = render(
      <MemoryRouter>
        <CityCard city={mapCity} fare={displayedFare} weekend="2027-01-08" mode="all" />
      </MemoryRouter>,
    );
    expect(screen.getByText(FLIXBUS_DISPLAYED_FARE_DISCLOSURE)).toBeVisible();
    card.unmount();

    const map = render(
      <MemoryRouter>
        <CrowlMap cities={[mapCity]} fares={[displayedFare]} weekend="2027-01-08" mode="all" />
      </MemoryRouter>,
    );
    const marker = screen.getByRole('link', {
      name: new RegExp(`Vienna, €55, ${FLIXBUS_DISPLAYED_FARE_DISCLOSURE}`),
    });
    fireEvent.mouseEnter(marker);
    expect(screen.getAllByText(FLIXBUS_DISPLAYED_FARE_DISCLOSURE).length).toBeGreaterThan(0);
    map.unmount();

    const journey: Journey = {
      id: 'flixbus-revision', totalPriceCents: 5_500, currency: 'EUR', mode: 'bus',
      observedAt: '2026-11-01T02:00:00Z', currentObservedAt: '2026-11-01T02:00:00Z',
      currentCrawlRunId: '42', currentDiagnostic: null,
      outboundDepartureAt: '2027-01-08T20:00:00Z', returnArrivalAt: '2027-01-11T05:00:00Z',
      coverage: 'complete', sourceScopeRatio: 1, freshness: 'fresh', availability: 'available', live: true,
      priceCompleteness: 'estimated', mandatoryFeesIncluded: false,
      fareDisclosure: FLIXBUS_DISPLAYED_FARE_DISCLOSURE,
      legs: [],
      bookings: [{
        id: 'flixbus-booking', seller: 'FlixBus', source: 'flixbus', priceCents: 5_500,
        currency: 'EUR', bookingUrl: null, fareName: 'Displayed fare', studentFare: false,
        priceCompleteness: 'estimated', mandatoryFeesIncluded: false,
        fareDisclosure: FLIXBUS_DISPLAYED_FARE_DISCLOSURE,
      }],
    };
    render(<ItineraryPanel journey={journey} />);
    expect(screen.getAllByText(FLIXBUS_DISPLAYED_FARE_DISCLOSURE)).toHaveLength(2);
    expect(screen.getByText('Link unavailable')).toBeInTheDocument();
  });

  it('shows a city that is both visited and wishlisted as visited only', () => {
    const dualStatusCity = { ...mapCity, status: { visited: true, wishlist: true } };
    render(
      <MemoryRouter>
        <CrowlMap cities={[dualStatusCity]} fares={[]} weekend="" mode="all" view={{ lat: 48.2, lon: 16.37, zoom: 5 }} />
      </MemoryRouter>,
    );

    const marker = screen.getByRole('link', { name: 'Vienna, visited. View city.' });
    expect(marker).not.toHaveClass('border-visited', 'border-wishlist');
    fireEvent.mouseEnter(marker);
    expect(screen.getByText('✓ Visited')).toBeInTheDocument();
    expect(screen.queryByText('☆ Wishlist')).not.toBeInTheDocument();
  });

  it('zooms and resets one shared viewport without detaching city links', async () => {
    const user = userEvent.setup();
    render(
      <MemoryRouter>
        <CrowlMap cities={[mapCity]} fares={[]} weekend="2027-01-08" mode="train" />
      </MemoryRouter>,
    );
    const stage = screen.getByTestId('map-stage');
    const basemap = screen.getByRole('region', { name: 'Interactive geographic map of Europe' });
    const marker = screen.getByRole('link', { name: /Vienna, collection failed\. View city\./i });

    expect(basemap).toHaveAccessibleName('Interactive geographic map of Europe');
    expect(basemap).toContainElement(marker);
    expect(marker).toHaveAttribute('href', '/cities/vienna?weekend=2027-01-08&mode=train');

    await user.click(screen.getByRole('button', { name: 'Zoom in' }));
    expect(stage).toHaveAttribute('data-zoom', '3.40');
    expect(screen.getByTestId('maplibre-map')).toHaveAttribute('data-map-zoom', '3.4');
    expect(screen.getByRole('button', { name: 'Reset map view' })).toBeEnabled();

    await user.click(screen.getByRole('button', { name: 'Reset map view' }));
    expect(stage).toHaveAttribute('data-zoom', '2.80');
    expect(screen.getByTestId('maplibre-map')).toHaveAttribute('data-longitude', '6.5');
  });

  it('exposes one native keyboard map surface without a duplicate outer tab stop', () => {
    render(
      <MemoryRouter>
        <CrowlMap cities={[mapCity]} fares={[]} weekend="" mode="all" />
      </MemoryRouter>,
    );
    const stage = screen.getByTestId('map-stage');
    const basemap = screen.getByRole('region', { name: 'Interactive geographic map of Europe' });

    expect(stage).not.toHaveAttribute('tabindex');
    expect(basemap).toHaveAttribute('tabindex', '0');
    basemap.focus();
    expect(basemap).toHaveFocus();
  });

  it('fits the catalog with marker padding on a 320px viewport', () => {
    vi.stubGlobal('innerWidth', 320);
    const reykjavik: City = {
      ...mapCity,
      id: 'reykjavik-is',
      slug: 'reykjavik',
      name: 'Reykjavík',
      coordinates: { lat: 64.1466, lon: -21.9426 },
    };
    const krakow: City = {
      ...mapCity,
      id: 'krakow-pl',
      slug: 'krakow',
      name: 'Kraków',
      coordinates: { lat: 50.0647, lon: 19.945 },
    };
    render(
      <MemoryRouter>
        <CrowlMap cities={[reykjavik, krakow]} fares={[]} weekend="" mode="all" />
      </MemoryRouter>,
    );

    const map = screen.getByTestId('maplibre-map');
    const zoom = Number(map.getAttribute('data-map-zoom'));
    const longitude = Number(map.getAttribute('data-longitude'));
    const projectedX = (lon: number) => 134 + ((lon - longitude) / 360) * MAP_TILE_SIZE_FOR_TEST * (2 ** zoom);

    expect(zoom).toBeLessThan(1.7);
    expect(projectedX(reykjavik.coordinates.lon)).toBeGreaterThanOrEqual(39);
    expect(projectedX(krakow.coordinates.lon)).toBeLessThanOrEqual(229);
  });

  it('restores a shared map viewport and preserves it in city navigation', async () => {
    const onViewChange = vi.fn();
    render(
      <MemoryRouter>
        <CrowlMap
          cities={[mapCity]}
          fares={[]}
          weekend="2027-01-08"
          mode="train"
          view={{ lat: 48.2, lon: 16.37, zoom: 4.2 }}
          navigationQuery="weekend=2027-01-08&mode=train&lat=48.200&lon=16.370&zoom=4.20"
          onViewChange={onViewChange}
        />
      </MemoryRouter>,
    );
    const stage = screen.getByTestId('map-stage');

    await waitFor(() => expect(stage).toHaveAttribute('data-zoom', '4.20'));
    expect(screen.getByRole('link', { name: /Vienna, collection failed\. View city\./i }))
      .toHaveAttribute('href', '/cities/vienna?weekend=2027-01-08&mode=train&lat=48.200&lon=16.370&zoom=4.20');
    await waitFor(() => expect(onViewChange).toHaveBeenCalled());
    expect(onViewChange.mock.calls.at(-1)?.[0]).toMatchObject({ zoom: 4.2, lat: 48.2, lon: 16.37 });
  });

  it('clusters dense projected cities deterministically and reveals focused member links', async () => {
    const user = userEvent.setup();
    const city = (slug: string, name: string, lat: number, lon: number): City => ({
      ...mapCity,
      id: `${slug}-id`,
      slug,
      name,
      coordinates: { lat, lon },
      photo: { ...mapCity.photo, alt: name },
    });
    const alpha = city('alpha', 'Alpha', 48, 16);
    const beta = city('beta', 'Beta', 48.02, 16.2);
    const gamma = city('gamma', 'Gamma', 48.04, 16.4);
    const distant = city('distant', 'Distant', 38.7, -9.1);

    render(
      <MemoryRouter>
        <CrowlMap cities={[gamma, distant, alpha, beta]} fares={[]} weekend="" mode="all" />
      </MemoryRouter>,
    );
    const stage = screen.getByTestId('map-stage');
    const cluster = screen.getByRole('button', { name: /3 nearby cities: Alpha, Beta, Gamma\. Zoom in to show city links\./ });

    expect(screen.getByRole('link', { name: 'Distant, wishlist. View city.' })).toBeInTheDocument();
    expect(screen.queryByRole('link', { name: 'Alpha, wishlist. View city.' })).not.toBeInTheDocument();

    await user.click(cluster);

    expect(stage).toHaveAttribute('data-zoom', '5.00');
    const alphaLink = await screen.findByRole('link', { name: 'Alpha, wishlist. View city.' });
    expect(screen.getByRole('link', { name: 'Beta, wishlist. View city.' })).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Gamma, wishlist. View city.' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /3 nearby cities/ })).not.toBeInTheDocument();
    await waitFor(() => expect(alphaLink).toHaveFocus());
  });

  it('keeps unobserved heatmap positions in the arrow-key grid', async () => {
    const user = userEvent.setup();
    const onSelect = vi.fn();
    render(
      <PriceHeatmap
        history={{
          cells: [
            { weekend: '2027-01-08', observedOn: '2027-01-01', leadDays: 7, priceCents: 8_900, availability: 'available', coverage: 'complete', sourceScopeRatio: 1, freshness: 'fresh', observedAt: '2027-01-01T02:00:00Z', currentObservedAt: '2027-01-01T02:00:00Z', currentCrawlRunId: '1', currentDiagnostic: null },
            { weekend: '2027-01-15', observedOn: '2027-01-15', leadDays: 0, priceCents: 9_900, availability: 'available', coverage: 'complete', sourceScopeRatio: 1, freshness: 'fresh', observedAt: '2027-01-15T02:00:00Z', currentObservedAt: '2027-01-15T02:00:00Z', currentCrawlRunId: '2', currentDiagnostic: null },
          ],
          scale: { p10Cents: 8_900, p90Cents: 9_900 },
        }}
        scheduledWeekends={['2027-01-08', '2027-01-15']}
        onSelect={onSelect}
      />,
    );

    const cells = screen.getAllByRole('button');
    expect(cells).toHaveLength(4);
    cells[0].focus();

    await user.keyboard('{ArrowRight}');
    expect(cells[1]).toHaveFocus();
    expect(cells[1]).toHaveAttribute('aria-disabled', 'true');

    await user.keyboard('{Enter}{ArrowDown}');
    expect(onSelect).not.toHaveBeenCalled();
    expect(cells[3]).toHaveFocus();
  });

  it('distinguishes future observation dates from missed past crawls', () => {
    const day = 24 * 60 * 60 * 1000;
    const asDate = (offset: number) => new Date(Date.now() + offset * day).toISOString().slice(0, 10);
    render(
      <PriceHeatmap
        history={{ cells: [], scale: { p10Cents: 5_000, p90Cents: 25_000 } }}
        scheduledWeekends={[asDate(-14), asDate(14)]}
        onSelect={vi.fn()}
      />,
    );

    expect(screen.getAllByRole('button', { name: /observation date .* has not arrived yet/i }).length).toBeGreaterThan(0);
    expect(screen.getAllByRole('button', { name: /expected crawl observation is missing/i }).length).toBeGreaterThan(0);
  });

  it('bounds the default weekend view and pages the accessible table', async () => {
    const user = userEvent.setup();
    const day = 24 * 60 * 60 * 1000;
    const asDate = (offset: number) => new Date(Date.now() + offset * day).toISOString().slice(0, 10);
    const past = Array.from({ length: 30 }, (_, index) => asDate(-(index + 5) * 7));
    const upcoming = Array.from({ length: 30 }, (_, index) => asDate((index + 1) * 7));
    const observedWeekend = upcoming[0];
    render(
      <PriceHeatmap
        history={{
          cells: [{
            weekend: observedWeekend, observedOn: observedWeekend, leadDays: 0,
            priceCents: 8_900, availability: 'available', coverage: 'complete',
            sourceScopeRatio: 1, freshness: 'fresh', observedAt: `${observedWeekend}T02:00:00Z`,
            currentObservedAt: `${observedWeekend}T02:00:00Z`, currentCrawlRunId: '1', currentDiagnostic: null,
          }],
          scale: { p10Cents: 8_900, p90Cents: 8_900 },
        }}
        scheduledWeekends={[...past, ...upcoming]}
        onSelect={vi.fn()}
      />,
    );

    expect(screen.getByText(/Showing 52 of 60 trip weekends/i)).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'Show all 60 weekends' }));
    expect(screen.getByText(/Showing 60 of 60 trip weekends/i)).toBeInTheDocument();

    await user.click(screen.getByText('Open accessible price table'));
    const table = screen.getByRole('table');
    expect(within(table).getAllByRole('row')).toHaveLength(13);
    expect(screen.getByText('Page 1 of 5')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'Next table page' }));
    expect(screen.getByText('Page 2 of 5')).toBeInTheDocument();
  });

  it('samples dense daily lead times instead of rendering 181 columns', () => {
    const weekend = new Date(Date.now() + 200 * 24 * 60 * 60 * 1000).toISOString().slice(0, 10);
    render(
      <PriceHeatmap
        history={{
          cells: Array.from({ length: 181 }, (_, leadDays) => ({
            weekend, observedOn: weekend, leadDays,
            priceCents: 9_000 + leadDays, availability: 'available' as const, coverage: 'complete' as const,
            sourceScopeRatio: 1, freshness: 'fresh' as const, observedAt: `${weekend}T02:00:00Z`,
            currentObservedAt: `${weekend}T02:00:00Z`, currentCrawlRunId: '1', currentDiagnostic: null,
          })),
          scale: { p10Cents: 9_000, p90Cents: 9_180 },
        }}
        onSelect={vi.fn()}
      />,
    );

    expect(screen.getByText(/18 representative lead times are shown from 181 observed dates/i)).toBeInTheDocument();
    expect(within(screen.getByRole('region', { name: 'Historical price heatmap' })).getAllByRole('button')).toHaveLength(18);
  });

  it('explains a collection failure instead of presenting it as no eligible trip', () => {
    render(
      <ItineraryPanel
        journey={null}
        emptyState={{ availability: 'unknown_due_failure', coverage: 'failed', freshness: 'never_observed', observedAt: null, currentObservedAt: '2027-01-01T04:00:00Z', currentCrawlRunId: '17', currentDiagnostic: 'UPSTREAM_FAILURE' }}
      />,
    );

    expect(screen.getByRole('heading', { name: 'Journey status could not be verified' })).toBeInTheDocument();
    expect(screen.getByText('Collection failed')).toBeInTheDocument();
    expect(screen.getByText(/Latest check .*Collection failed.*batch 17.*UPSTREAM_FAILURE/)).toBeInTheDocument();
    expect(screen.queryByRole('heading', { name: 'No eligible journey found' })).not.toBeInTheDocument();
  });

  it('never exposes booking links for a non-live revision', () => {
    const journey: Journey = {
      id: 'revision-12',
      totalPriceCents: 12_300,
      currency: 'EUR',
      mode: 'train',
      observedAt: '2026-11-01T02:00:00Z',
      currentObservedAt: '2026-11-02T03:00:00Z',
      currentCrawlRunId: '18',
      currentDiagnostic: null,
      outboundDepartureAt: '2027-01-08T20:00:00Z',
      returnArrivalAt: '2027-01-11T05:00:00Z',
      coverage: 'complete',
      sourceScopeRatio: 1,
      freshness: 'stale',
      availability: 'available',
      live: false,
      legs: [],
      bookings: [{
        id: 'booking-1', seller: 'Rail seller', source: 'rail', priceCents: 12_300, currency: 'EUR',
        bookingUrl: 'https://example.com/book', fareName: 'Youth', studentFare: true,
      }],
    };

    render(<ItineraryPanel journey={journey} />);

    expect(screen.queryByRole('link', { name: 'Check with seller' })).not.toBeInTheDocument();
    expect(screen.getByText('Historical only')).toBeInTheDocument();
  });

  it('suppresses booking links when the latest availability is unverified', () => {
    const journey: Journey = {
      id: 'revision-13', totalPriceCents: 12_300, currency: 'EUR', mode: 'train',
      observedAt: '2026-11-01T02:00:00Z', currentObservedAt: '2026-11-02T03:00:00Z',
      currentCrawlRunId: '19', currentDiagnostic: 'UPSTREAM_FAILURE',
      outboundDepartureAt: '2027-01-08T20:00:00Z', returnArrivalAt: '2027-01-11T05:00:00Z',
      coverage: 'failed', sourceScopeRatio: 1, freshness: 'fresh',
      availability: 'unknown_due_failure', live: true, legs: [],
      bookings: [{ id: 'booking-2', seller: 'Rail seller', source: 'rail', priceCents: 12_300, currency: 'EUR', bookingUrl: 'https://example.com/book', studentFare: false, bookableAtObservation: true }],
    };

    render(<ItineraryPanel journey={journey} />);

    expect(screen.queryByRole('link', { name: 'Check with seller' })).not.toBeInTheDocument();
    expect(screen.getByText('Latest availability unverified')).toBeInTheDocument();
  });

  it('suppresses an expired booking link even for a fresh available journey', () => {
    const journey: Journey = {
      id: 'revision-14', totalPriceCents: 12_300, currency: 'EUR', mode: 'train',
      observedAt: '2026-11-01T02:00:00Z', currentObservedAt: '2026-11-01T02:00:00Z',
      currentCrawlRunId: '20', currentDiagnostic: null,
      outboundDepartureAt: '2027-01-08T20:00:00Z', returnArrivalAt: '2027-01-11T05:00:00Z',
      coverage: 'complete', sourceScopeRatio: 1, freshness: 'fresh', availability: 'available', live: true, legs: [],
      bookings: [{ id: 'booking-3', seller: 'Rail seller', source: 'rail', priceCents: 12_300, currency: 'EUR', bookingUrl: 'https://example.com/book', studentFare: false, bookableAtObservation: true, expiresAt: '2000-01-01T00:00:00Z' }],
    };

    render(<ItineraryPanel journey={journey} />);

    expect(screen.queryByRole('link', { name: 'Check with seller' })).not.toBeInTheDocument();
    expect(screen.getByText('Offer link expired')).toBeInTheDocument();
  });

  it('chooses the nearest nonpast weekend even when the response is unordered', () => {
    const weekend = (friday: string, endsAt: string): Weekend => ({
      friday,
      startsAt: `${friday}T19:00:00+01:00`,
      endsAt,
      label: friday,
      minimumStayHours: 12,
      maxDirectionHours: 12,
    });
    const weekends = [
      weekend('2027-01-22', '2027-01-25T07:00:00+01:00'),
      weekend('2027-01-08', '2027-01-11T07:00:00+01:00'),
      weekend('2027-01-15', '2027-01-18T07:00:00+01:00'),
    ];

    expect(nextNonpastWeekend(weekends, Date.parse('2027-01-12T12:00:00Z'))).toBe('2027-01-15');
  });
});
