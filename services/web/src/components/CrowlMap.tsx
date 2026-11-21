import {
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from 'react';
import { Map, Marker, type ViewStateChangeEvent } from '@vis.gl/react-maplibre';
import { Link } from 'react-router-dom';
import type { City, MapFare, Mode } from '../types';
import {
  availabilityLabel,
  categoryLabel,
  formatCurrency,
  formatDateTime,
  joinClassNames,
  modeLabel,
  FLIXBUS_DISPLAYED_FARE_DISCLOSURE,
  priceBand,
} from '../utils';
import { AvailabilityPill, CoveragePill, FreshnessPill } from './StatusPill';

interface CrowlMapProps {
  cities: City[];
  fares: MapFare[];
  weekend: string;
  mode: Mode;
  view?: MapView;
  navigationQuery?: string;
  onViewChange?: (view: MapView) => void;
  categoryLabels?: Record<string, string>;
  attributeLabels?: Record<string, string>;
  controls?: ReactNode;
  dataBadge?: string;
}

export interface MapView {
  lat: number;
  lon: number;
  zoom: number;
}

export const DEFAULT_MAP_VIEW: MapView = { lat: 50.6, lon: 6.5, zoom: 2.8 };
export const MIN_MAP_ZOOM = 1.4;
export const MAX_MAP_ZOOM = 8;
const HOME_COORDINATES = { lat: 48.7145, lon: 2.2457 };
const MAP_TILE_SIZE = 512;

const mercatorY = (lat: number) => {
  const radians = Math.max(-85, Math.min(85, lat)) * Math.PI / 180;
  return (1 - Math.log(Math.tan(radians) + (1 / Math.cos(radians))) / Math.PI) / 2;
};

const latitudeFromMercatorY = (y: number) => (
  Math.atan(Math.sinh(Math.PI * (1 - (2 * y)))) * 180 / Math.PI
);

const narrowCatalogView = (cities: City[], viewportWidth: number): MapView => {
  const points = [HOME_COORDINATES, ...cities.map((city) => city.coordinates)];
  const xs = points.map((point) => (point.lon + 180) / 360);
  const ys = points.map((point) => mercatorY(point.lat));
  const minX = Math.min(...xs);
  const maxX = Math.max(...xs);
  const minY = Math.min(...ys);
  const maxY = Math.max(...ys);
  const availableWidth = Math.max(120, viewportWidth - 80);
  const availableHeight = 440;
  const zoomX = Math.log2(availableWidth / (MAP_TILE_SIZE * Math.max(maxX - minX, 0.0001)));
  const zoomY = Math.log2(availableHeight / (MAP_TILE_SIZE * Math.max(maxY - minY, 0.0001)));

  return {
    lat: latitudeFromMercatorY((minY + maxY) / 2),
    lon: ((minX + maxX) / 2) * 360 - 180,
    zoom: Math.max(MIN_MAP_ZOOM, Math.min(2.45, zoomX, zoomY)),
  };
};

export const defaultMapViewForViewport = (cities: City[] = []): MapView => {
  if (typeof window === 'undefined') return DEFAULT_MAP_VIEW;
  if (window.innerWidth <= 640 && cities.length) {
    const mapWidth = Math.max(240, window.innerWidth - 52);
    return narrowCatalogView(cities, mapWidth);
  }
  if (window.innerWidth <= 420) return { lat: 50.6, lon: 0, zoom: 1.9 };
  if (window.innerWidth <= 640) return { lat: 50.6, lon: 2.5, zoom: 2.45 };
  return DEFAULT_MAP_VIEW;
};

const MAP_STYLE = (import.meta.env.VITE_MAP_STYLE_URL as string | undefined)
  ?? 'https://tiles.openfreemap.org/styles/positron';
const ZOOM_STEP = 0.6;
const CLUSTER_REVEAL_ZOOM = 5;

interface MarkerGroup {
  key: string;
  members: City[];
  lat: number;
  lon: number;
}

interface MapState {
  latitude: number;
  longitude: number;
  zoom: number;
}

const toMapState = (view: MapView): MapState => ({
  latitude: Math.max(32, Math.min(72, view.lat)),
  longitude: Math.max(-32, Math.min(42, view.lon)),
  zoom: Math.max(MIN_MAP_ZOOM, Math.min(MAX_MAP_ZOOM, view.zoom)),
});

const viewsMatch = (left: MapState, right: MapState) => (
  Math.abs(left.latitude - right.latitude) < 0.0005
  && Math.abs(left.longitude - right.longitude) < 0.0005
  && Math.abs(left.zoom - right.zoom) < 0.005
);

const markerGroups = (cities: City[], zoom: number): MarkerGroup[] => {
  const points = cities
    .map((city) => ({
      city,
      lat: city.coordinates.lat,
      lon: city.coordinates.lon,
      x: city.coordinates.lon * Math.cos((city.coordinates.lat * Math.PI) / 180),
      y: city.coordinates.lat,
    }))
    .sort((a, b) => a.city.slug.localeCompare(b.city.slug));

  if (zoom >= CLUSTER_REVEAL_ZOOM) {
    return points.map((point) => ({
      key: point.city.slug,
      members: [point.city],
      lat: point.lat,
      lon: point.lon,
    }));
  }

  const radius = 2.45 / Math.pow(2, Math.max(0, zoom - DEFAULT_MAP_VIEW.zoom));
  let clusters = points.map((point) => ({
    key: point.city.slug,
    points: [point],
    x: point.x,
    y: point.y,
  }));

  while (true) {
    let closest: { left: number; right: number; distance: number; key: string } | null = null;
    for (let left = 0; left < clusters.length; left += 1) {
      for (let right = left + 1; right < clusters.length; right += 1) {
        const distance = Math.hypot(clusters[left].x - clusters[right].x, clusters[left].y - clusters[right].y);
        const key = `${clusters[left].key}::${clusters[right].key}`;
        if (distance > radius) continue;
        if (!closest || distance < closest.distance || (distance === closest.distance && key.localeCompare(closest.key) < 0)) {
          closest = { left, right, distance, key };
        }
      }
    }
    if (!closest) break;

    const mergedPoints = [
      ...clusters[closest.left].points,
      ...clusters[closest.right].points,
    ].sort((a, b) => a.city.slug.localeCompare(b.city.slug));
    const merged = {
      key: mergedPoints.map((point) => point.city.slug).join('|'),
      points: mergedPoints,
      x: mergedPoints.reduce((sum, point) => sum + point.x, 0) / mergedPoints.length,
      y: mergedPoints.reduce((sum, point) => sum + point.y, 0) / mergedPoints.length,
    };
    clusters = clusters.filter((_, index) => index !== closest?.left && index !== closest?.right);
    clusters.push(merged);
    clusters.sort((a, b) => a.key.localeCompare(b.key));
  }

  return clusters.map((cluster) => ({
    key: cluster.key,
    members: cluster.points.map((point) => point.city),
    lat: cluster.points.reduce((sum, point) => sum + point.lat, 0) / cluster.points.length,
    lon: cluster.points.reduce((sum, point) => sum + point.lon, 0) / cluster.points.length,
  }));
};

export const CrowlMap = ({
  cities,
  fares,
  weekend,
  mode,
  view,
  navigationQuery,
  onViewChange,
  categoryLabels = {},
  attributeLabels = {},
  controls,
  dataBadge,
}: CrowlMapProps) => {
  const fareMap = useMemo(() => new globalThis.Map(fares.map((fare) => [fare.citySlug, fare])), [fares]);
  const [activeSlug, setActiveSlug] = useState<string | null>(null);
  const [mapLoadFailed, setMapLoadFailed] = useState(false);
  const mapHasLoaded = useRef(false);
  const initialDefaultView = useRef(defaultMapViewForViewport(cities));
  const [mapView, setMapView] = useState<MapState>(() => toMapState(view ?? initialDefaultView.current));
  const cityLinkRefs = useRef(new globalThis.Map<string, HTMLAnchorElement>());
  const pendingFocusSlug = useRef<string | null>(null);
  const groups = useMemo(() => markerGroups(cities, mapView.zoom), [cities, mapView.zoom]);
  const activeCity = cities.find((city) => city.slug === activeSlug);
  const activeFare = activeCity ? fareMap.get(activeCity.slug) : undefined;
  const query = new URLSearchParams(navigationQuery ?? '');
  if (navigationQuery === undefined) {
    if (weekend) query.set('weekend', weekend);
    if (mode !== 'all') query.set('mode', mode);
  }

  useEffect(() => {
    const desired = toMapState(view ?? initialDefaultView.current);
    setMapView((current) => viewsMatch(current, desired) ? current : desired);
  }, [view?.lat, view?.lon, view?.zoom]);

  useEffect(() => {
    if (!onViewChange) return;
    const timeout = window.setTimeout(() => onViewChange({
      lat: mapView.latitude,
      lon: mapView.longitude,
      zoom: mapView.zoom,
    }), 250);
    return () => window.clearTimeout(timeout);
  }, [mapView.latitude, mapView.longitude, mapView.zoom, onViewChange]);

  useEffect(() => {
    const slug = pendingFocusSlug.current;
    if (!slug || mapView.zoom < CLUSTER_REVEAL_ZOOM) return;
    const link = cityLinkRefs.current.get(slug);
    if (!link) return;
    pendingFocusSlug.current = null;
    setActiveSlug(slug);
    link.focus();
  }, [groups, mapView.zoom]);

  const changeZoom = (amount: number) => {
    setMapView((current) => ({
      ...current,
      zoom: Math.max(MIN_MAP_ZOOM, Math.min(MAX_MAP_ZOOM, current.zoom + amount)),
    }));
  };

  const resetView = () => setMapView(toMapState(defaultMapViewForViewport(cities)));

  const handleMapMove = (event: ViewStateChangeEvent) => {
    setMapView(toMapState({
      lat: event.viewState.latitude,
      lon: event.viewState.longitude,
      zoom: event.viewState.zoom,
    }));
  };

  const revealGroup = (group: MarkerGroup) => {
    pendingFocusSlug.current = group.members[0]?.slug ?? null;
    setMapView({
      latitude: group.lat,
      longitude: group.lon,
      zoom: Math.min(MAX_MAP_ZOOM, Math.max(CLUSTER_REVEAL_ZOOM, mapView.zoom + 1.5)),
    });
  };

  const isDefaultView = viewsMatch(mapView, toMapState(defaultMapViewForViewport(cities)));

  return (
    <section className="map-card" aria-labelledby="map-title">
      <h1 id="map-title" className="sr-only">Schengen travel map</h1>
      <div className="map-toolbar">
        {controls}
        <div className="map-meta">
          {dataBadge && <span className="data-badge" title="The city catalog or fares contain development data.">{dataBadge}</span>}
          <span>{cities.length} cities</span>
        </div>
      </div>
      <div
        className="map-stage"
        data-testid="map-stage"
        data-zoom={mapView.zoom.toFixed(2)}
      >
        {!mapLoadFailed && (
          <>
            <Map
          longitude={mapView.longitude}
          latitude={mapView.latitude}
          zoom={mapView.zoom}
          bearing={0}
          pitch={0}
          minZoom={MIN_MAP_ZOOM}
          maxZoom={MAX_MAP_ZOOM}
          mapStyle={MAP_STYLE}
          renderWorldCopies={false}
          dragRotate={false}
          pitchWithRotate={false}
          touchPitch={false}
          attributionControl={{ compact: true }}
          locale={{ 'Map.Title': 'Interactive geographic map of Europe' }}
          reuseMaps
          onMove={handleMapMove}
          onLoad={() => {
            mapHasLoaded.current = true;
            setMapLoadFailed(false);
          }}
          onError={() => {
            if (!mapHasLoaded.current) setMapLoadFailed(true);
          }}
          style={{ position: 'absolute', inset: 0 }}
        >
          <Marker longitude={HOME_COORDINATES.lon} latitude={HOME_COORDINATES.lat} anchor="left" offset={[-22, 0]} className="map-marker-layer home-marker-layer">
            <div className="home-marker" aria-label="Home base: Palaiseau and Paris">
              <span className="home-dot" aria-hidden="true">⌂</span>
              <span>Palaiseau / Paris</span>
            </div>
          </Marker>

          {groups.map((group) => {
            if (group.members.length > 1) {
              const namedMembers = group.members.slice(0, 3).map((city) => city.name);
              const memberSummary = `${namedMembers.join(', ')}${group.members.length > namedMembers.length ? `, and ${group.members.length - namedMembers.length} more` : ''}`;
              const label = `${group.members.length} nearby cities: ${memberSummary}. Zoom in to show city links.`;
              return (
                <Marker longitude={group.lon} latitude={group.lat} anchor="center" key={group.key} className="map-marker-layer cluster-marker-layer">
                  <button
                    type="button"
                    className="city-cluster"
                    aria-label={label}
                    title={label}
                    onFocus={() => setActiveSlug(group.members[0].slug)}
                    onClick={() => revealGroup(group)}
                  >
                    <strong>{group.members.length}</strong>
                    <span>cities</span>
                  </button>
                </Marker>
              );
            }

            const city = group.members[0];
            const fare = fareMap.get(city.slug);
            const hasPrice = Boolean(weekend && fare?.priceCents !== null && fare?.priceCents !== undefined);
            const markerClass = weekend
              ? hasPrice ? priceBand(fare?.priceCents ?? null) : fare ? `fare-${fare.availability}` : 'marker-missing'
              : city.status.visited ? 'marker-visited' : city.status.wishlist ? 'marker-wishlist' : 'marker-neutral';
            const markerText = weekend
              ? fare?.priceCents !== null && fare?.priceCents !== undefined ? formatCurrency(fare.priceCents) : '—'
              : city.status.visited ? '✓' : city.status.wishlist ? '☆' : '·';
            const availability = fare?.availability ?? 'unknown_due_failure';
            const cityStatus = city.status.visited ? 'visited' : city.status.wishlist ? 'wishlist' : 'monitored';
            const fareDescription = hasPrice
              ? `${formatCurrency(fare?.priceCents ?? null)}${fare?.fareDisclosure === FLIXBUS_DISPLAYED_FARE_DISCLOSURE ? `, ${FLIXBUS_DISPLAYED_FARE_DISCLOSURE}` : ''}${availability !== 'available' ? `, latest check ${availabilityLabel[availability]}` : ''}`
              : availabilityLabel[availability];

            return (
              <Marker longitude={city.coordinates.lon} latitude={city.coordinates.lat} anchor="center" key={city.slug} className="map-marker-layer city-marker-layer">
                <Link
                  ref={(element) => {
                    if (element) cityLinkRefs.current.set(city.slug, element);
                    else cityLinkRefs.current.delete(city.slug);
                  }}
                  className={joinClassNames(
                    'city-marker',
                    markerClass,
                    weekend && fare?.coverage === 'partial' && 'is-partial-coverage',
                    weekend && fare?.freshness === 'stale' && 'is-stale-fare',
                    weekend && Boolean(fare && fare.sourceScopeRatio < 1) && 'is-limited-scope',
                  )}
                  to={`/cities/${city.slug}${query.size ? `?${query}` : ''}`}
                  onMouseEnter={() => setActiveSlug(city.slug)}
                  onFocus={() => setActiveSlug(city.slug)}
                  aria-label={`${city.name}, ${weekend ? fareDescription : cityStatus}. View city.`}
                  title={weekend && fare?.fareDisclosure === FLIXBUS_DISPLAYED_FARE_DISCLOSURE ? `${formatCurrency(fare.priceCents)} · ${FLIXBUS_DISPLAYED_FARE_DISCLOSURE}` : undefined}
                >
                  <span>{markerText}</span>
                </Link>
              </Marker>
            );
          })}
            </Map>

            <div className="map-controls" role="group" aria-label="Map view controls">
              <button type="button" aria-label="Zoom in" title="Zoom in" onClick={() => changeZoom(ZOOM_STEP)} disabled={mapView.zoom >= MAX_MAP_ZOOM}><span aria-hidden="true">+</span></button>
              <button type="button" aria-label="Zoom out" title="Zoom out" onClick={() => changeZoom(-ZOOM_STEP)} disabled={mapView.zoom <= MIN_MAP_ZOOM}><span aria-hidden="true">−</span></button>
              <button type="button" aria-label="Reset map view" title="Reset map view" onClick={resetView} disabled={isDefaultView}><span aria-hidden="true">⌂</span></button>
            </div>

            {activeCity && (
              <aside className="map-tooltip" aria-live="polite">
            <div className="tooltip-heading">
              <div><strong>{activeCity.name}</strong><span>{activeCity.country.name}</span></div>
              {activeFare?.winningMode && <span className="mode-badge">{modeLabel(activeFare.winningMode)}</span>}
            </div>
            <p>{activeCity.summary}</p>
            <div className="chip-row">
              {activeCity.status.visited
                ? <span className="mini-chip chip-visited">✓ Visited</span>
                : activeCity.status.wishlist && <span className="mini-chip chip-wishlist">☆ Wishlist</span>}
              {activeCity.categories.slice(0, 3).map((item) => <span className="mini-chip" key={item}>{categoryLabels[item] ?? categoryLabel(item)}</span>)}
            </div>
            <div className="tooltip-attributes" aria-label={`${activeCity.name} attributes`}>
              {Object.entries(activeCity.attributes).map(([key, value]) => (
                <span key={key}><small>{attributeLabels[key] ?? categoryLabel(key)}</small><strong>{value}/5</strong></span>
              ))}
            </div>
            {weekend && activeFare && (
              <>
                <div className="tooltip-price"><strong>{formatCurrency(activeFare.priceCents)}</strong><span>{availabilityLabel[activeFare.availability]}</span></div>
                {activeFare.fareDisclosure === FLIXBUS_DISPLAYED_FARE_DISCLOSURE && <p className="fare-disclosure">{FLIXBUS_DISPLAYED_FARE_DISCLOSURE}</p>}
                <div className="pill-row"><AvailabilityPill value={activeFare.availability} /><CoveragePill value={activeFare.coverage} /><FreshnessPill value={activeFare.freshness} /></div>
                <p className="tooltip-scope">Requested-source scope: {Math.round(activeFare.sourceScopeRatio * 100)}%</p>
                {activeFare.originTerminal && <p className="tooltip-route">
                  <span>Out: {activeFare.originTerminal} → {activeFare.destinationTerminal}</span>
                  {activeFare.returnOriginTerminal && <span>Back: {activeFare.returnOriginTerminal} → {activeFare.returnDestinationTerminal}</span>}
                </p>}
                {activeFare.observedAt && <p className="tooltip-observation">Retained fare observed {formatDateTime(activeFare.observedAt)}</p>}
                {activeFare.currentObservedAt && (
                  <p className="tooltip-current-state">
                    Latest check {formatDateTime(activeFare.currentObservedAt)}: {availabilityLabel[activeFare.availability]}
                    {activeFare.currentCrawlRunId && ` · batch ${activeFare.currentCrawlRunId}`}
                    {activeFare.currentDiagnostic && ` · ${activeFare.currentDiagnostic}`}
                  </p>
                )}
              </>
            )}
            <Link className="text-link" to={`/cities/${activeCity.slug}${query.size ? `?${query}` : ''}`}>View {activeCity.name}</Link>
              </aside>
            )}
          </>
        )}

        {mapLoadFailed && (
          <div className="map-load-error" role="alert">Map unavailable. Use the city directory below.</div>
        )}
      </div>
      <div className="map-legend" aria-label="Map legend">
        {weekend ? (
          <>
            <span className="legend-title">Round trip</span>
            <span><i className="legend-swatch price-1" /> under €50</span>
            <span><i className="legend-swatch price-2" /> €50–99</span>
            <span><i className="legend-swatch price-3" /> €100–174</span>
            <span><i className="legend-swatch price-4" /> €175–249</span>
            <span><i className="legend-swatch price-5" /> €250+</span>
            <span><i className="legend-swatch fare-not_on_sale" /> not on sale</span>
            <span><i className="legend-swatch fare-sold_out" /> sold out</span>
            <span><i className="legend-swatch fare-unknown_due_failure" /> failed</span>
            <span><i className="legend-outline legend-partial" /> partial</span>
            <span><i className="legend-outline legend-stale" /> stale</span>
            {fares.some((fare) => fare.fareDisclosure === FLIXBUS_DISPLAYED_FARE_DISCLOSURE) && <span className="fare-disclosure">{FLIXBUS_DISPLAYED_FARE_DISCLOSURE}</span>}
          </>
        ) : (
          <>
            <span className="legend-title">Status</span>
            <span><i className="legend-swatch marker-visited" /> visited</span>
            <span><i className="legend-swatch marker-wishlist" /> wishlist</span>
            <span><i className="legend-swatch home-legend" /> home</span>
          </>
        )}
      </div>
    </section>
  );
};
