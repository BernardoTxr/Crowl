import { useCallback, useDeferredValue, useEffect, useMemo, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { useSearchParams } from 'react-router-dom';
import { api } from '../api/client';
import { CityCard } from '../components/CityCard';
import {
  CrowlMap,
  MAX_MAP_ZOOM,
  MIN_MAP_ZOOM,
  defaultMapViewForViewport,
  type MapView,
} from '../components/CrowlMap';
import { TravelFilters } from '../components/Filters';
import { ErrorBlock, LoadingBlock } from '../components/Layout';
import type { City, MapFare, Mode } from '../types';
import { safeMode } from '../utils';

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
  const cities = citiesQuery.data?.data ?? [];

  useEffect(() => {
    document.title = 'Crowl — Schengen weekend explorer';
  }, []);

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
    setSearchParams(next);
    if (key === 'weekend') setSort(value ? 'price' : 'status');
  };

  const updateMapView = useCallback((view: MapView) => {
    const next = new URLSearchParams(searchParams);
    const defaultView = defaultMapViewForViewport(cities);
    const isDefaultView = Math.abs(view.zoom - defaultView.zoom) < 0.01
      && Math.abs(view.lat - defaultView.lat) < 0.01
      && Math.abs(view.lon - defaultView.lon) < 0.01;
    if (isDefaultView) {
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
  }, [cities, searchParams, setSearchParams]);

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
  const isSynthetic = Boolean(faresQuery.data?.isSynthetic);
  const isLoading = citiesQuery.isLoading || metaQuery.isLoading || weekendsQuery.isLoading || (weekend && faresQuery.isLoading);
  const hasCatalog = Boolean(citiesQuery.data && metaQuery.data && weekendsQuery.data);
  const error = citiesQuery.error || metaQuery.error || weekendsQuery.error || faresQuery.error;

  return (
    <div className="page-shell home-page">
      {error && <ErrorBlock message={error instanceof Error ? error.message : 'Please try again.'} />}

      {isLoading ? <LoadingBlock label="Loading map…" /> : hasCatalog ? (
        <>
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
            dataBadge={isSynthetic ? 'Synthetic data' : undefined}
            controls={(
              <TravelFilters
                weekends={weekendsQuery.data?.data ?? []}
                weekend={weekend}
                mode={mode}
                onWeekendChange={(value) => updateFilter('weekend', value)}
                onModeChange={(value) => updateFilter('mode', value)}
                allowCollection
                compact
              />
            )}
          />

          <section className="city-directory" aria-labelledby="directory-title">
            <div className="section-heading">
              <h2 id="directory-title">City directory</h2>
            </div>
            <div className="directory-controls">
              <div className="search-field"><span aria-hidden="true">⌕</span><label className="sr-only" htmlFor="city-search">Search cities</label><input id="city-search" type="search" value={search} onChange={(event) => setSearch(event.target.value)} placeholder="Search by city, country, or category" /></div>
              <div className="sort-control"><label htmlFor="city-sort">Sort by</label><div className="select-wrap"><select id="city-sort" value={sort} onChange={(event) => setSort(event.target.value as Sort)}><option value="price">Lowest price</option><option value="name">Name</option><option value="status">Visit status</option></select></div></div>
            </div>
            <p className="sr-only" aria-live="polite">Showing {visibleCities.length} of {cities.length} monitored cities</p>
            {visibleCities.length ? (
              <div className="city-grid">
                {visibleCities.map((city) => <CityCard key={city.slug} city={city} fare={fareMap.get(city.slug)} weekend={weekend} mode={mode} categoryLabels={categoryLabels} />)}
              </div>
            ) : (
              <div className="empty-state"><strong>No matching city</strong><span>Try a country name or another category.</span></div>
            )}
          </section>
        </>
      ) : null}
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
  if (zoom === null || lat === null || lon === null) return undefined;
  return {
    zoom: Math.max(MIN_MAP_ZOOM, Math.min(MAX_MAP_ZOOM, zoom)),
    lat: Math.max(32, Math.min(72, lat)),
    lon: Math.max(-32, Math.min(42, lon)),
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
