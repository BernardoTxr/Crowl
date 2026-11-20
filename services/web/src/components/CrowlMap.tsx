import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type CSSProperties,
  type KeyboardEvent,
  type PointerEvent,
  type WheelEvent,
} from 'react';
import { Link } from 'react-router-dom';
import type { City, MapFare, Mode } from '../types';
import { availabilityLabel, categoryLabel, formatCurrency, formatDateTime, joinClassNames, modeLabel, priceBand } from '../utils';
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
}

export interface MapView {
  lat: number;
  lon: number;
  zoom: number;
}

const projectPoint = (lat: number, lon: number) => ({
  x: Math.max(20, Math.min(980, ((lon + 25) / 57) * 1000)),
  y: Math.max(18.6, Math.min(601.4, ((67 - lat) / 33) * 620)),
});

const project = (lat: number, lon: number) => {
  const point = projectPoint(lat, lon);
  return { left: `${point.x / 10}%`, top: `${point.y / 6.2}%` };
};

const MIN_ZOOM = 1;
const MAX_ZOOM = 3;
const ZOOM_STEP = .25;
const CLUSTER_REVEAL_ZOOM = 2.25;
const CLUSTER_RADIUS = 60;

interface MapViewport {
  scale: number;
  x: number;
  y: number;
}

interface MarkerGroup {
  key: string;
  members: City[];
  x: number;
  y: number;
}

const initialViewport: MapViewport = { scale: MIN_ZOOM, x: 0, y: 0 };

const clampViewport = (viewport: MapViewport, width: number, height: number): MapViewport => {
  const scale = Math.max(MIN_ZOOM, Math.min(MAX_ZOOM, viewport.scale));
  if (scale === MIN_ZOOM || !width || !height) return { scale, x: 0, y: 0 };
  return {
    scale,
    x: Math.max(width * (1 - scale), Math.min(0, viewport.x)),
    y: Math.max(height * (1 - scale), Math.min(0, viewport.y)),
  };
};

const viewportForView = (view: MapView, width: number, height: number): MapViewport => {
  const scale = Math.max(MIN_ZOOM, Math.min(MAX_ZOOM, view.zoom));
  const point = projectPoint(view.lat, view.lon);
  return clampViewport({
    scale,
    x: width / 2 - (point.x / 1000) * width * scale,
    y: height / 2 - (point.y / 620) * height * scale,
  }, width, height);
};

const viewForViewport = (viewport: MapViewport, width: number, height: number): MapView => {
  const projectedX = ((width / 2 - viewport.x) / viewport.scale / width) * 1000;
  const projectedY = ((height / 2 - viewport.y) / viewport.scale / height) * 620;
  return {
    lat: Math.max(34, Math.min(67, 67 - (projectedY / 620) * 33)),
    lon: Math.max(-25, Math.min(32, (projectedX / 1000) * 57 - 25)),
    zoom: viewport.scale,
  };
};

const isOverlayTarget = (target: EventTarget | null) =>
  target instanceof Element && Boolean(target.closest('a, button, .map-tooltip, .map-controls'));

const markerGroups = (cities: City[], scale: number): MarkerGroup[] => {
  const points = cities
    .map((city) => ({ city, ...projectPoint(city.coordinates.lat, city.coordinates.lon) }))
    .sort((a, b) => a.city.slug.localeCompare(b.city.slug));
  if (scale >= CLUSTER_REVEAL_ZOOM) {
    return points
      .map((point) => ({ key: point.city.slug, members: [point.city], x: point.x, y: point.y }))
      .sort((a, b) => a.y - b.y || a.x - b.x || a.key.localeCompare(b.key));
  }

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
        const distance = Math.hypot(clusters[left].x - clusters[right].x, clusters[left].y - clusters[right].y) * scale;
        const key = `${clusters[left].key}::${clusters[right].key}`;
        if (distance > CLUSTER_RADIUS) continue;
        if (!closest || distance < closest.distance || (distance === closest.distance && key.localeCompare(closest.key) < 0)) {
          closest = { left, right, distance, key };
        }
      }
    }
    if (!closest) break;
    const left = clusters[closest.left];
    const right = clusters[closest.right];
    const mergedPoints = [...left.points, ...right.points].sort((a, b) => a.city.slug.localeCompare(b.city.slug));
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

  return clusters
    .map((cluster) => ({
      key: cluster.key,
      members: cluster.points.map((point) => point.city),
      x: cluster.x,
      y: cluster.y,
    }))
    .sort((a, b) => a.y - b.y || a.x - b.x || a.key.localeCompare(b.key));
};

export const CrowlMap = ({ cities, fares, weekend, mode, view, navigationQuery, onViewChange, categoryLabels = {}, attributeLabels = {} }: CrowlMapProps) => {
  const fareMap = useMemo(() => new Map(fares.map((fare) => [fare.citySlug, fare])), [fares]);
  const [activeSlug, setActiveSlug] = useState<string | null>(null);
  const [viewport, setViewport] = useState<MapViewport>(initialViewport);
  const [isDragging, setIsDragging] = useState(false);
  const stageRef = useRef<HTMLDivElement | null>(null);
  const dragRef = useRef<{ pointerId: number; startX: number; startY: number; originX: number; originY: number } | null>(null);
  const cityLinkRefs = useRef(new Map<string, HTMLAnchorElement>());
  const pendingFocusSlug = useRef<string | null>(null);
  const groups = useMemo(() => markerGroups(cities, viewport.scale), [cities, viewport.scale]);
  const activeCity = cities.find((city) => city.slug === activeSlug) ?? cities[0];
  const activeFare = activeCity ? fareMap.get(activeCity.slug) : undefined;
  const query = new URLSearchParams(navigationQuery ?? '');
  if (navigationQuery === undefined) {
    if (weekend) query.set('weekend', weekend);
    if (mode !== 'all') query.set('mode', mode);
  }

  const stageSize = useCallback(() => {
    const rect = stageRef.current?.getBoundingClientRect();
    return { width: rect?.width ?? 0, height: rect?.height ?? 0, left: rect?.left ?? 0, top: rect?.top ?? 0 };
  }, []);

  const zoomTo = useCallback((requestedScale: number, anchor?: { x: number; y: number }) => {
    const size = stageSize();
    setViewport((current) => {
      const scale = Math.max(MIN_ZOOM, Math.min(MAX_ZOOM, requestedScale));
      if (scale === current.scale) return current;
      const focus = anchor ?? { x: size.width / 2, y: size.height / 2 };
      const mapX = (focus.x - current.x) / current.scale;
      const mapY = (focus.y - current.y) / current.scale;
      return clampViewport({
        scale,
        x: focus.x - mapX * scale,
        y: focus.y - mapY * scale,
      }, size.width, size.height);
    });
  }, [stageSize]);

  const zoomBy = useCallback((amount: number, anchor?: { x: number; y: number }) => {
    setViewport((current) => {
      const size = stageSize();
      const scale = Math.max(MIN_ZOOM, Math.min(MAX_ZOOM, current.scale + amount));
      if (scale === current.scale) return current;
      const focus = anchor ?? { x: size.width / 2, y: size.height / 2 };
      const mapX = (focus.x - current.x) / current.scale;
      const mapY = (focus.y - current.y) / current.scale;
      return clampViewport({
        scale,
        x: focus.x - mapX * scale,
        y: focus.y - mapY * scale,
      }, size.width, size.height);
    });
  }, [stageSize]);

  const panBy = useCallback((x: number, y: number) => {
    const size = stageSize();
    setViewport((current) => clampViewport({ ...current, x: current.x + x, y: current.y + y }, size.width, size.height));
  }, [stageSize]);

  const resetViewport = useCallback(() => {
    dragRef.current = null;
    setIsDragging(false);
    setViewport(initialViewport);
  }, []);

  useEffect(() => {
    if (!view) {
      setViewport(initialViewport);
      return;
    }
    const applyView = () => {
      const size = stageSize();
      if (!size.width || !size.height) return;
      setViewport(viewportForView(view, size.width, size.height));
    };
    applyView();
    window.addEventListener('resize', applyView);
    return () => window.removeEventListener('resize', applyView);
  }, [stageSize, view?.lat, view?.lon, view?.zoom]);

  useEffect(() => {
    if (!onViewChange) return;
    const timeout = window.setTimeout(() => {
      const size = stageSize();
      if (!size.width || !size.height) return;
      onViewChange(viewForViewport(viewport, size.width, size.height));
    }, 180);
    return () => window.clearTimeout(timeout);
  }, [onViewChange, stageSize, viewport]);

  const revealGroup = (group: MarkerGroup) => {
    const size = stageSize();
    pendingFocusSlug.current = group.members[0]?.slug ?? null;
    setViewport((current) => {
      const scale = Math.min(MAX_ZOOM, Math.max(CLUSTER_REVEAL_ZOOM, current.scale + 1));
      return clampViewport({
        scale,
        x: size.width / 2 - (group.x / 1000) * size.width * scale,
        y: size.height / 2 - (group.y / 620) * size.height * scale,
      }, size.width, size.height);
    });
  };

  useEffect(() => {
    const slug = pendingFocusSlug.current;
    if (!slug || viewport.scale < CLUSTER_REVEAL_ZOOM) return;
    const link = cityLinkRefs.current.get(slug);
    if (!link) return;
    pendingFocusSlug.current = null;
    setActiveSlug(slug);
    link.focus();
  }, [groups, viewport.scale]);

  const handleKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    if (event.target !== event.currentTarget) return;
    if (event.key === '+' || event.key === '=') zoomBy(ZOOM_STEP);
    else if (event.key === '-') zoomBy(-ZOOM_STEP);
    else if (event.key === 'ArrowLeft') panBy(56, 0);
    else if (event.key === 'ArrowRight') panBy(-56, 0);
    else if (event.key === 'ArrowUp') panBy(0, 56);
    else if (event.key === 'ArrowDown') panBy(0, -56);
    else if (event.key === 'Home' || event.key === '0') resetViewport();
    else return;
    event.preventDefault();
  };

  const handleWheel = (event: WheelEvent<HTMLDivElement>) => {
    if (!event.ctrlKey && !event.metaKey) return;
    event.preventDefault();
    const size = stageSize();
    zoomBy(event.deltaY < 0 ? ZOOM_STEP : -ZOOM_STEP, {
      x: event.clientX - size.left,
      y: event.clientY - size.top,
    });
  };

  const handlePointerDown = (event: PointerEvent<HTMLDivElement>) => {
    if (viewport.scale === MIN_ZOOM || event.button !== 0 || isOverlayTarget(event.target)) return;
    dragRef.current = {
      pointerId: event.pointerId,
      startX: event.clientX,
      startY: event.clientY,
      originX: viewport.x,
      originY: viewport.y,
    };
    event.currentTarget.setPointerCapture?.(event.pointerId);
    setIsDragging(true);
  };

  const handlePointerMove = (event: PointerEvent<HTMLDivElement>) => {
    const drag = dragRef.current;
    if (!drag || drag.pointerId !== event.pointerId) return;
    const size = stageSize();
    setViewport((current) => clampViewport({
      ...current,
      x: drag.originX + event.clientX - drag.startX,
      y: drag.originY + event.clientY - drag.startY,
    }, size.width, size.height));
  };

  const finishPointer = (event: PointerEvent<HTMLDivElement>) => {
    const drag = dragRef.current;
    if (!drag || drag.pointerId !== event.pointerId) return;
    event.currentTarget.releasePointerCapture?.(event.pointerId);
    dragRef.current = null;
    setIsDragging(false);
  };

  const viewportStyle = {
    transform: `translate3d(${viewport.x}px, ${viewport.y}px, 0) scale(${viewport.scale})`,
  } as CSSProperties;

  return (
    <section className="map-card" aria-labelledby="map-title">
      <div className="map-heading">
        <div>
          <p className="eyebrow">Schengen Area</p>
          <h2 id="map-title">{weekend ? 'Where can this weekend take you?' : 'Your travel constellation'}</h2>
        </div>
        <div className="map-count"><strong>{cities.length}</strong><span>monitored cities</span></div>
      </div>
      <p id="map-navigation-hint" className="sr-only">Use the map controls or plus and minus keys to zoom. When zoomed in, drag or use the arrow keys to pan. Press Home or zero to reset the view. Hold Control or Command while scrolling to zoom at the pointer.</p>
      <div
        ref={stageRef}
        className={joinClassNames('map-stage', viewport.scale > MIN_ZOOM && 'is-zoomed', isDragging && 'is-dragging')}
        role="region"
        aria-label="Interactive Schengen travel map"
        aria-describedby="map-navigation-hint"
        tabIndex={0}
        data-zoom={viewport.scale}
        onKeyDown={handleKeyDown}
        onWheel={handleWheel}
        onPointerDown={handlePointerDown}
        onPointerMove={handlePointerMove}
        onPointerUp={finishPointer}
        onPointerCancel={finishPointer}
      >
        <div className="map-controls" role="group" aria-label="Map view controls">
          <button type="button" aria-label="Zoom in" title="Zoom in" onClick={() => zoomTo(viewport.scale + ZOOM_STEP)} disabled={viewport.scale >= MAX_ZOOM}><span aria-hidden="true">+</span></button>
          <button type="button" aria-label="Zoom out" title="Zoom out" onClick={() => zoomTo(viewport.scale - ZOOM_STEP)} disabled={viewport.scale <= MIN_ZOOM}><span aria-hidden="true">−</span></button>
          <button type="button" aria-label="Reset map view" title="Reset map view" onClick={resetViewport} disabled={viewport.scale === MIN_ZOOM && viewport.x === 0 && viewport.y === 0}><span aria-hidden="true">⌂</span></button>
          <span className="map-zoom-readout" role="status" aria-live="polite">{Math.round(viewport.scale * 100)}%</span>
        </div>
        <div className="map-viewport" style={viewportStyle} data-testid="map-viewport">
          <svg className="europe-map" viewBox="0 0 1000 620" role="img" aria-labelledby="map-svg-title map-svg-desc">
          <title id="map-svg-title">Stylised map of the Schengen Area</title>
          <desc id="map-svg-desc">City markers link to monitored destinations. The list after the map provides the same information.</desc>
          <defs>
            <pattern id="map-grid" width="38" height="38" patternUnits="userSpaceOnUse">
              <path d="M 38 0 L 0 0 0 38" fill="none" stroke="#c8dfe6" strokeWidth="1" />
            </pattern>
          </defs>
          <rect width="1000" height="620" fill="#eaf7f7" rx="24" />
          <rect width="1000" height="620" fill="url(#map-grid)" rx="24" opacity=".55" />
          <g className="map-land">
            <path d="M90 170 155 123l65 14 48-36 72 15 18 55 54 14 18 65 61 6 40-28 75 13 20 62 61 21 50-31 80 31 32 77-57 49-5 87-73 25-45-39-60 22-44-45-58 19-50-37-57 19-55-30-66 17-30-48-83-13-31-64 28-60-17-56 42-53Z" />
            <path d="M744 54 814 29l73 26-22 42-65 15-56-58Z" />
            <path d="M514 502 548 493l17 42-20 60-24-9 8-38-15-46Z" />
            <path d="M167 347 218 330l13 76-29 75-58-4-25-51 48-79Z" />
            <path d="M25 65 82 35l47 22-25 43-55 21L25 65Z" />
          </g>
          <g className="map-water-labels" aria-hidden="true">
            <text x="65" y="280">ATLANTIC</text>
            <text x="430" y="575">MEDITERRANEAN</text>
            <text x="775" y="170">BALTIC</text>
          </g>
          </svg>

          <div className="map-markers" aria-label="Map city links">
            <div className="home-marker" style={project(48.7145, 2.2457)}>
              <span className="home-dot" aria-hidden="true">⌂</span>
              <span>Palaiseau / Paris</span>
            </div>
            {groups.map((group) => {
              if (group.members.length > 1) {
                const namedMembers = group.members.slice(0, 3).map((city) => city.name);
                const memberSummary = `${namedMembers.join(', ')}${group.members.length > namedMembers.length ? `, and ${group.members.length - namedMembers.length} more` : ''}`;
                const label = `${group.members.length} nearby cities: ${memberSummary}. Zoom in to show city links.`;
                return (
                  <button
                    type="button"
                    className="city-cluster"
                    style={{ left: `${group.x / 10}%`, top: `${group.y / 6.2}%` }}
                    key={group.key}
                    aria-label={label}
                    title={label}
                    onFocus={() => setActiveSlug(group.members[0].slug)}
                    onClick={() => revealGroup(group)}
                  >
                    <strong>{group.members.length}</strong>
                    <span>cities</span>
                  </button>
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
                : city.status.visited ? '✓' : '☆';
              return (
                <Link
                  key={city.slug}
                  ref={(element) => {
                    if (element) cityLinkRefs.current.set(city.slug, element);
                    else cityLinkRefs.current.delete(city.slug);
                  }}
                  className={joinClassNames(
                    'city-marker',
                    markerClass,
                    city.status.visited && 'border-visited',
                    city.status.wishlist && 'border-wishlist',
                    weekend && fare?.coverage === 'partial' && 'is-partial-coverage',
                    weekend && fare?.freshness === 'stale' && 'is-stale-fare',
                    weekend && Boolean(fare && fare.sourceScopeRatio < 1) && 'is-limited-scope',
                  )}
                  style={project(city.coordinates.lat, city.coordinates.lon)}
                  to={`/cities/${city.slug}${query.size ? `?${query}` : ''}`}
                  onMouseEnter={() => setActiveSlug(city.slug)}
                  onFocus={() => setActiveSlug(city.slug)}
                  aria-label={`${city.name}, ${weekend ? fare?.priceCents ? `${formatCurrency(fare.priceCents)}${fare.availability !== 'available' ? `, latest check ${availabilityLabel[fare.availability]}` : ''}` : availabilityLabel[fare?.availability ?? 'unknown_due_failure'] : city.status.visited ? 'visited' : 'wishlist'}. View city.`}
                >
                  <span>{markerText}</span>
                </Link>
              );
            })}
          </div>
        </div>

        {activeCity && (
          <aside className="map-tooltip" aria-live="polite">
            <div className="tooltip-heading">
              <div><strong>{activeCity.name}</strong><span>{activeCity.country.name}</span></div>
              {activeFare?.winningMode && <span className="mode-badge">{modeLabel(activeFare.winningMode)}</span>}
            </div>
            <p>{activeCity.summary}</p>
            <div className="chip-row">
              {activeCity.status.visited && <span className="mini-chip chip-visited">✓ Visited</span>}
              {activeCity.status.wishlist && <span className="mini-chip chip-wishlist">☆ Wishlist</span>}
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
      </div>
      <div className="map-legend" aria-label="Map legend">
        {weekend ? (
          <>
            <span className="legend-title">Round-trip fare</span>
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
            <span><i className="legend-outline outline-visited" /> visited</span>
            <span><i className="legend-outline outline-wishlist" /> wishlist</span>
          </>
        ) : (
          <>
            <span className="legend-title">City status</span>
            <span><i className="legend-swatch marker-visited" /> visited</span>
            <span><i className="legend-swatch marker-wishlist" /> wishlist</span>
            <span><i className="legend-swatch home-legend" /> home base</span>
          </>
        )}
      </div>
    </section>
  );
};
