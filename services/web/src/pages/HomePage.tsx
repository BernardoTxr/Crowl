import { useCallback, useDeferredValue, useEffect, useMemo, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { Link, useSearchParams } from 'react-router-dom';
import { api } from '../api/client';
import { CityCard } from '../components/CityCard';
import { CrowlMap, type MapView } from '../components/CrowlMap';
import { TravelFilters } from '../components/Filters';
import { DemoNotice, ErrorBlock, LoadingBlock } from '../components/Layout';
import type { City, MapFare, Mode } from '../types';
import { formatCurrency, safeMode } from '../utils';

type Sort = 'price' | 'name' | 'status';

export const HomePage = () => {
  const [searchParams, setSearchParams] = useSearchParams();
  const weekend = searchParams.get('weekend') ?? '';
  const mode = safeMode(searchParams.get('mode'));
  const [search, setSearch] = useState('');
  const deferredSearch = useDeferredValue(search.trim().toLocaleLowerCase());
  const [sort, setSort] = useState<Sort>(weekend ? 'price' : 'status');
  const mapView = useMemo(() => mapViewFromParams(searchParams), [searchParams]);

  const citiesQuery = useQuery({ queryKey: ['cities'], queryFn: api.getCities });
  const metaQuery = useQuery({ queryKey: ['meta'], queryFn: api.getMeta });
  const weekendsQuery = useQuery({ queryKey: ['weekends'], queryFn: api.getWeekends });
  const faresQuery = useQuery({
    queryKey: ['map-fares', weekend, mode],
    queryFn: () => api.getMapFares(weekend, mode),
    enabled: Boolean(weekend),
  });

  useEffect(() => {
    document.title = 'Crowl — Schengen weekend explorer';
  }, []);

  const updateFilter = (key: 'weekend' | 'mode', value: string) => {
    const next = new URLSearchParams(searchParams);
    if (!value || (key === 'mode' && value === 'all')) next.delete(key);
    else next.set(key, value);
    setSearchParams(next);
    if (key === 'weekend') setSort(value ? 'price' : 'status');
  };

  const updateMapView = useCallback((view: MapView) => {
    const next = new URLSearchParams(searchParams);
    if (view.zoom <= 1.001) {
      next.delete('lat');
      next.delete('lon');
      next.delete('zoom');
    } else {
      next.set('lat', view.lat.toFixed(3));
      next.set('lon', view.lon.toFixed(3));
      next.set('zoom', view.zoom.toFixed(2));
    }
    if (next.toString() !== searchParams.toString()) {
      setSearchParams(next, { replace: true });
    }
  }, [searchParams, setSearchParams]);

  const cities = citiesQuery.data?.data ?? [];
  const categoryLabels = useMemo(() => Object.fromEntries(
    (metaQuery.data?.data.categoryDefinitions ?? []).map((definition) => [definition.id, definition.label]),
  ), [metaQuery.data]);
  const attributeLabels = useMemo(() => Object.fromEntries(
    (metaQuery.data?.data.attributeDefinitions ?? []).map((definition) => [definition.id, definition.label]),
  ), [metaQuery.data]);
  const fares = faresQuery.data?.data ?? [];
  const fareMap = useMemo(() => new Map(fares.map((fare) => [fare.citySlug, fare])), [fares]);
  const visibleCities = useMemo(() => sortCities(
    cities.filter((city) => !deferredSearch || `${city.name} ${city.country.name} ${city.categories.map((category) => categoryLabels[category] ?? category).join(' ')}`.toLocaleLowerCase().includes(deferredSearch)),
    fareMap,
    sort,
  ), [cities, categoryLabels, deferredSearch, fareMap, sort]);
  const pricedFares = fares.filter((fare) => fare.priceCents !== null);
  const lowest = pricedFares.reduce<MapFare | null>((best, item) => !best || (item.priceCents ?? Infinity) < (best.priceCents ?? Infinity) ? item : best, null);
  const isMock = Boolean(citiesQuery.data?.isMock || metaQuery.data?.isMock || weekendsQuery.data?.isMock || faresQuery.data?.isMock);
  const isMockCatalog = metaQuery.data?.data.catalogVariant === 'mock';
  const isSynthetic = Boolean(faresQuery.data?.isSynthetic);
  const isLoading = citiesQuery.isLoading || metaQuery.isLoading || weekendsQuery.isLoading || (weekend && faresQuery.isLoading);
  const error = citiesQuery.error || metaQuery.error || weekendsQuery.error || faresQuery.error;

  return (
    <div className="page-shell home-page">
      <section className="home-hero">
        <div className="hero-copy">
          <p className="eyebrow">From Palaiseau / Paris</p>
          <h1>Find a weekend worth <em>flying, riding, or rolling</em> for.</h1>
          <p className="hero-lede">Compare complete round trips to the cities on your list—across air, rail, and bus—without hiding the actual terminals.</p>
        </div>
        <div className="hero-stamp" aria-label="Six month outlook">
          <span>Looking ahead</span>
          <strong>6</strong>
          <span>months of weekends</span>
        </div>
      </section>

      {isSynthetic && isMockCatalog ? <DemoNotice kind="catalog-synthetic" /> : isSynthetic ? <DemoNotice kind="synthetic" /> : isMockCatalog ? <DemoNotice kind="catalog" /> : isMock && <DemoNotice />}
      {error && <ErrorBlock message={error instanceof Error ? error.message : 'Please try again.'} />}
      <TravelFilters
        weekends={weekendsQuery.data?.data ?? []}
        weekend={weekend}
        mode={mode}
        onWeekendChange={(value) => updateFilter('weekend', value)}
        onModeChange={(value) => updateFilter('mode', value)}
        allowCollection
      />

      {weekend && (
        <section className="overview-strip" aria-label="Weekend overview">
          <div><span>Eligible destinations</span><strong>{pricedFares.length}<small> / {cities.length}</small></strong></div>
          <div><span>Lowest observed</span><strong>{formatCurrency(lowest?.priceCents)}</strong></div>
          <div><span>Travel window</span><strong>60 hours</strong><small>Fri 19:00 → Mon 07:00</small></div>
          <div><span>Price includes</span><strong>Round trip</strong><small>Mandatory fees · no extras</small></div>
        </section>
      )}

      {isLoading ? <LoadingBlock label="Mapping the next weekends…" /> : (
        <CrowlMap
          cities={cities}
          fares={fares}
          weekend={weekend}
          mode={mode}
          view={mapView}
          navigationQuery={searchParams.toString()}
          onViewChange={updateMapView}
          categoryLabels={categoryLabels}
          attributeLabels={attributeLabels}
        />
      )}

      <aside className="fare-disclosure">
        <span aria-hidden="true">i</span>
        <p><strong>{isSynthetic ? 'Contains development fixtures.' : 'What these prices mean.'}</strong> {isSynthetic && 'Synthetic amounts are generated examples, not live fares. '}Round trip for one traveler, age 22/student where supported; mandatory fees included and no extra baggage. Travel from Palaiseau to the departure terminal and from the arrival terminal into the destination city is excluded. Transfers shown inside an itinerary are included where priced.</p>
      </aside>

      <section className="city-directory" aria-labelledby="directory-title">
        <div className="section-heading">
          <div><p className="eyebrow">Accessible map alternative</p><h2 id="directory-title">City directory</h2></div>
          <Link to="/status" className="outline-button">Check source health</Link>
        </div>
        <div className="directory-controls">
          <div className="search-field"><span aria-hidden="true">⌕</span><label className="sr-only" htmlFor="city-search">Search cities</label><input id="city-search" type="search" value={search} onChange={(event) => setSearch(event.target.value)} placeholder="Search by city, country, or category" /></div>
          <div className="sort-control"><label htmlFor="city-sort">Sort by</label><div className="select-wrap"><select id="city-sort" value={sort} onChange={(event) => setSort(event.target.value as Sort)}><option value="price">Lowest price</option><option value="name">Name</option><option value="status">Visit status</option></select></div></div>
        </div>
        <p className="results-count" aria-live="polite">Showing {visibleCities.length} of {cities.length} monitored cities</p>
        {visibleCities.length ? (
          <div className="city-grid">
            {visibleCities.map((city) => <CityCard key={city.slug} city={city} fare={fareMap.get(city.slug)} weekend={weekend} mode={mode} categoryLabels={categoryLabels} />)}
          </div>
        ) : (
          <div className="empty-state"><strong>No matching city</strong><span>Try a country name or another category.</span></div>
        )}
      </section>
    </div>
  );
};

const finiteParam = (params: URLSearchParams, key: string) => {
  const raw = params.get(key);
  if (raw === null || raw.trim() === '') return null;
  const value = Number(raw);
  return Number.isFinite(value) ? value : null;
};

const mapViewFromParams = (params: URLSearchParams): MapView | undefined => {
  const zoom = finiteParam(params, 'zoom');
  const lat = finiteParam(params, 'lat');
  const lon = finiteParam(params, 'lon');
  if (zoom === null || lat === null || lon === null || zoom <= 1) return undefined;
  return {
    zoom: Math.max(1, Math.min(3, zoom)),
    lat: Math.max(34, Math.min(67, lat)),
    lon: Math.max(-25, Math.min(32, lon)),
  };
};

const sortCities = (cities: City[], fares: Map<string, MapFare>, sort: Sort) => [...cities].sort((a, b) => {
  if (sort === 'price') {
    const priceA = fares.get(a.slug)?.priceCents ?? Infinity;
    const priceB = fares.get(b.slug)?.priceCents ?? Infinity;
    if (priceA !== priceB) return priceA - priceB;
  }
  if (sort === 'status') {
    const score = (city: City) => city.status.visited ? 0 : city.status.wishlist ? 1 : 2;
    if (score(a) !== score(b)) return score(a) - score(b);
  }
  return a.name.localeCompare(b.name);
});
