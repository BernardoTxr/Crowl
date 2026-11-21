import type {
  ApiResult,
  Availability,
  Batch,
  BatchState,
  BatchStatus,
  BookingComponent,
  CatalogDefinition,
  CatalogMeta,
  City,
  Coverage,
  Extractor,
  ExtractorCounts,
  ExtractorState,
  Freshness,
  HeatmapCell,
  Journey,
  JourneyLeg,
  MapFare,
  Mode,
  Photo,
  PriceCompleteness,
  PriceHistory,
  TravelMode,
  TypicalDuration,
  Weekend,
} from '../types';
import { asNumber, asString } from '../utils';

const API_BASE = ((import.meta.env.VITE_API_BASE_URL as string | undefined) ?? '/api/v1').replace(/\/$/, '');

type UnknownRecord = Record<string, unknown>;

const isRecord = (value: unknown): value is UnknownRecord =>
  typeof value === 'object' && value !== null && !Array.isArray(value);

const record = (value: unknown): UnknownRecord => isRecord(value) ? value : {};
const first = (...values: unknown[]) => values.find((value) => value !== null && value !== undefined);
const stringOrNull = (value: unknown) => typeof value === 'string' && value ? value : null;
const bool = (value: unknown, fallback = false) => typeof value === 'boolean' ? value : fallback;
const safeDiagnostic = (value: unknown) => {
  const diagnostic = stringOrNull(value)?.trim() ?? null;
  return diagnostic && /^[A-Za-z0-9][A-Za-z0-9_.:,|= -]{0,159}$/.test(diagnostic)
    ? diagnostic
    : null;
};

const nestedString = (value: unknown, ...keys: string[]) => {
  if (typeof value === 'string') return value;
  const item = record(value);
  for (const key of keys) {
    if (typeof item[key] === 'string') return item[key] as string;
  }
  return '';
};

const normalizeMode = (value: unknown, fallback: TravelMode = 'flight'): TravelMode =>
  value === 'train' || value === 'bus' || value === 'mixed' || value === 'flight' ? value : fallback;

const normalizeAvailability = (value: unknown, hasPrice = false): Availability => {
  const allowed: Availability[] = ['available', 'no_valid_option', 'not_on_sale', 'sold_out', 'unknown_due_failure'];
  return allowed.includes(value as Availability) ? value as Availability : hasPrice ? 'available' : 'unknown_due_failure';
};

const normalizeCoverage = (value: unknown): Coverage => {
  const allowed: Coverage[] = ['complete', 'partial', 'failed', 'not_applicable'];
  return allowed.includes(value as Coverage) ? value as Coverage : 'partial';
};

const normalizeFreshness = (value: unknown): Freshness => value === 'never_observed' ? 'never_observed' : value === 'stale' ? 'stale' : 'fresh';
const normalizePriceCompleteness = (value: unknown): PriceCompleteness =>
  value === 'estimated' ? 'estimated' : value === 'unknown' ? 'unknown' : 'complete';

const unwrapItems = (payload: unknown): unknown[] => {
  if (Array.isArray(payload)) return payload;
  const data = record(payload);
  const possible = first(data.items, data.results, data.cities, data.cells, data.extractors);
  return Array.isArray(possible) ? possible : [];
};

interface FetchResult {
  ok: boolean;
  payload: unknown;
  status?: number;
}

const request = async (path: string): Promise<FetchResult> => {
  const controller = new AbortController();
  const timeout = window.setTimeout(() => controller.abort(), 7_500);
  try {
    const response = await fetch(`${API_BASE}${path}`, {
      headers: { Accept: 'application/json' },
      signal: controller.signal,
    });
    if (!response.ok) return { ok: false, payload: null, status: response.status };
    return { ok: true, payload: await response.json(), status: response.status };
  } catch {
    return { ok: false, payload: null };
  } finally {
    window.clearTimeout(timeout);
  }
};

const requireResponse = (response: FetchResult, resource: string) => {
  if (response.ok) return;
  const suffix = response.status ? ` (HTTP ${response.status})` : '';
  throw new Error(`Crowl could not load ${resource}${suffix}.`);
};

const normalizePhoto = (value: unknown, cityName: string): Photo => {
  const item = record(value);
  return {
    asset: asString(first(item.asset, item.url, item.asset_path), '/crowl-mark.svg'),
    alt: asString(item.alt, `View of ${cityName}`),
    creator: stringOrNull(item.creator),
    sourceUrl: stringOrNull(first(item.sourceUrl, item.source_url)),
    license: stringOrNull(item.license),
    objectPosition: asString(first(item.objectPosition, item.object_position), '50% 50%'),
  };
};

const normalizeAttributes = (value: unknown) => {
  if (isRecord(value)) {
    return Object.fromEntries(Object.entries(value).flatMap(([key, item]) => {
      if (typeof item === 'number') return [[key, item]];
      const detail = record(item);
      const rating = first(detail.value, detail.rating, detail.score);
      return typeof rating === 'number' ? [[key, rating]] : [];
    }));
  }
  if (!Array.isArray(value)) return {};
  return Object.fromEntries(value.flatMap((entry) => {
    const item = record(entry);
    const key = stringOrNull(first(item.id, item.key, item.slug));
    const rating = first(item.value, item.rating, item.score);
    return key && typeof rating === 'number' ? [[key, rating]] : [];
  }));
};

const normalizeCategories = (value: unknown) => {
  if (!Array.isArray(value)) return { ids: [] as string[], labels: [] as string[] };
  const ids: string[] = [];
  const labels: string[] = [];
  value.forEach((entry) => {
    if (typeof entry === 'string') {
      ids.push(entry);
      return;
    }
    const item = record(entry);
    const id = stringOrNull(first(item.id, item.slug, item.key));
    const label = stringOrNull(item.label);
    if (id) ids.push(id);
    if (label) labels.push(label);
  });
  return { ids, labels };
};

const normalizeTypicalDurations = (value: unknown): TypicalDuration[] => {
  if (!Array.isArray(value)) return [];
  return value.map((entry) => {
    const item = record(entry);
    const median = first(item.median_minutes, item.medianMinutes, item.p50_minutes);
    const p25 = first(item.p25_minutes, item.p25Minutes);
    const p75 = first(item.p75_minutes, item.p75Minutes);
    return {
      mode: normalizeMode(item.mode),
      medianMinutes: typeof median === 'number' ? median : null,
      p25Minutes: typeof p25 === 'number' ? p25 : null,
      p75Minutes: typeof p75 === 'number' ? p75 : null,
      sampleCount: asNumber(first(item.sample_count, item.sampleCount)),
      isSynthetic: bool(first(item.is_synthetic, item.isSynthetic)),
    };
  });
};

const normalizeDefinition = (value: unknown): CatalogDefinition | null => {
  const item = record(value);
  const id = stringOrNull(item.id);
  const label = stringOrNull(item.label);
  if (!id || !label) return null;
  const minimum = first(item.min, item.minimum);
  const maximum = first(item.max, item.maximum);
  return {
    id,
    label,
    min: typeof minimum === 'number' ? minimum : null,
    max: typeof maximum === 'number' ? maximum : null,
    lowLabel: stringOrNull(first(item.low_label, item.lowLabel)),
    highLabel: stringOrNull(first(item.high_label, item.highLabel)),
  };
};

const normalizeCatalogMeta = (value: unknown): CatalogMeta => {
  const item = record(value);
  const categories = first(item.category_definitions, item.categoryDefinitions);
  const attributes = first(item.attribute_definitions, item.attributeDefinitions);
  if (first(item.catalog_variant, item.catalogVariant) !== 'final') {
    throw new Error('Crowl received an unsupported city catalog.');
  }
  return {
    catalogVariant: 'final',
    categoryDefinitions: (Array.isArray(categories) ? categories : [])
      .map(normalizeDefinition)
      .filter((definition): definition is CatalogDefinition => definition !== null),
    attributeDefinitions: (Array.isArray(attributes) ? attributes : [])
      .map(normalizeDefinition)
      .filter((definition): definition is CatalogDefinition => definition !== null),
  };
};

const normalizeCity = (value: unknown): City | null => {
  const item = record(value);
  const cityValue = isRecord(item.city) ? item.city : item;
  const slug = stringOrNull(first(cityValue.slug, cityValue.city_slug));
  const name = stringOrNull(cityValue.name);
  if (!slug || !name) return null;
  const country = record(cityValue.country);
  const coordinates = record(first(cityValue.coordinates, cityValue.coordinate));
  const status = record(cityValue.status);
  const categories = normalizeCategories(cityValue.categories);
  const countryCode = stringOrNull(first(country.code, cityValue.country_code));
  const countryName = stringOrNull(first(country.name, cityValue.country_name));
  const timezone = stringOrNull(cityValue.timezone);
  const latitude = first(coordinates.lat, coordinates.latitude, cityValue.latitude);
  const longitude = first(coordinates.lon, coordinates.lng, coordinates.longitude, cityValue.longitude);
  if (!countryCode || !countryName || !timezone || typeof latitude !== 'number' || typeof longitude !== 'number') return null;
  const attributes = normalizeAttributes(cityValue.attributes);
  return {
    id: asString(first(cityValue.id, cityValue.city_id), slug),
    slug,
    name,
    country: {
      code: countryCode,
      name: countryName,
    },
    timezone,
    coordinates: {
      lat: latitude,
      lon: longitude,
    },
    status: {
      visited: bool(first(status.visited, cityValue.visited)),
      wishlist: bool(first(status.wishlist, cityValue.wishlist)),
    },
    categories: categories.ids,
    categoryLabels: categories.labels,
    summary: asString(cityValue.summary),
    attributes,
    photo: normalizePhoto(first(item.photo, cityValue.photo), name),
    typicalDurations: normalizeTypicalDurations(first(item.typical_durations, item.typicalDurations, cityValue.typical_durations)),
  };
};

const normalizeWeekend = (value: unknown): Weekend | null => {
  const item = record(value);
  const friday = stringOrNull(first(item.friday, item.weekend));
  if (!friday) return null;
  const startsAt = asString(first(item.starts_at, item.startsAt), `${friday}T19:00:00+01:00`);
  const fridayDate = new Date(`${friday}T12:00:00Z`);
  const monday = new Date(fridayDate);
  monday.setUTCDate(monday.getUTCDate() + 3);
  return {
    friday,
    startsAt,
    endsAt: asString(first(item.ends_at, item.endsAt), `${monday.toISOString().slice(0, 10)}T07:00:00+01:00`),
    label: asString(item.label, friday),
    minimumStayHours: asNumber(first(item.minimum_stay_hours, item.minimumStayHours), 12),
    maxDirectionHours: asNumber(first(item.max_direction_hours, item.maxDirectionHours), 12),
  };
};

export const api = {
  async getMeta(): Promise<ApiResult<CatalogMeta>> {
    const response = await request('/meta');
    requireResponse(response, 'the catalog metadata');
    return { data: normalizeCatalogMeta(response.payload) };
  },

  async getWeekends(): Promise<ApiResult<Weekend[]>> {
    const response = await request('/weekends');
    requireResponse(response, 'the weekend calendar');
    const items = unwrapItems(response.payload).map(normalizeWeekend).filter((item): item is Weekend => item !== null);
    return { data: items };
  },

  async getCities(): Promise<ApiResult<City[]>> {
    const response = await request('/cities');
    requireResponse(response, 'the city catalog');
    const items = unwrapItems(response.payload).map(normalizeCity).filter((item): item is City => item !== null);
    return { data: items };
  },

  async getCity(slug: string): Promise<ApiResult<City | null>> {
    const response = await request(`/cities/${encodeURIComponent(slug)}`);
    if (response.status === 404) return { data: null };
    requireResponse(response, `${slug}'s city profile`);
    return { data: normalizeCity(response.payload) };
  },

  async getMapFares(weekend: string, mode: Mode): Promise<ApiResult<MapFare[]>> {
    const params = new URLSearchParams({ weekend, mode });
    const response = await request(`/map-prices?${params}`);
    requireResponse(response, 'map fares');
    const items: MapFare[] = unwrapItems(response.payload).flatMap((entry) => {
      const item = record(entry);
      const city = record(item.city);
      const slug = stringOrNull(first(item.city_slug, item.slug, city.slug));
      if (!slug) return [];
      const priceValue = first(item.price_eur_cents, item.price_cents, item.priceCents);
      const priceCents = typeof priceValue === 'number' ? priceValue : null;
      const ruleVersionId = first(item.rule_version_id, item.ruleVersionId);
      const profileId = first(item.profile_id, item.profileId);
      return [{
        citySlug: slug,
        priceCents,
        winningMode: priceCents === null ? null : normalizeMode(first(item.winning_mode, item.mode)),
        availability: normalizeAvailability(item.availability, priceCents !== null),
        coverage: normalizeCoverage(first(item.execution_coverage, item.coverage)),
        sourceScopeRatio: asNumber(first(item.source_scope_ratio, item.sourceScopeRatio)),
        isSynthetic: bool(first(item.is_synthetic, item.isSynthetic)),
        freshness: normalizeFreshness(item.freshness),
        observedAt: stringOrNull(first(item.observed_at, item.observedAt)),
        currentObservedAt: stringOrNull(first(item.current_observed_at, item.currentObservedAt)),
        currentCrawlRunId: asString(first(item.current_crawl_run_id, item.currentCrawlRunId)) || null,
        currentDiagnostic: safeDiagnostic(first(item.current_diagnostic, item.currentDiagnostic)),
        originTerminal: nestedString(first(item.origin_terminal, item.originTerminal), 'name', 'label') || null,
        destinationTerminal: nestedString(first(item.destination_terminal, item.destinationTerminal), 'name', 'label') || null,
        returnOriginTerminal: nestedString(first(item.return_origin_terminal, item.returnOriginTerminal), 'name', 'label') || null,
        returnDestinationTerminal: nestedString(first(item.return_destination_terminal, item.returnDestinationTerminal), 'name', 'label') || null,
        ruleVersionId: typeof ruleVersionId === 'number' ? ruleVersionId : null,
        profileId: typeof profileId === 'number' ? profileId : null,
        priceCompleteness: priceCents === null ? null : normalizePriceCompleteness(first(item.price_completeness, item.priceCompleteness)),
        mandatoryFeesIncluded: priceCents === null
          ? null
          : bool(first(item.mandatory_fees_included, item.mandatoryFeesIncluded), true),
        fareDisclosure: stringOrNull(first(item.fare_disclosure, item.fareDisclosure)),
      }];
    });
    return { data: items, isSynthetic: items.some((item) => item.isSynthetic) };
  },

  async getPriceHistory(slug: string, mode: Mode): Promise<ApiResult<PriceHistory>> {
    const params = new URLSearchParams({ mode });
    const response = await request(`/cities/${encodeURIComponent(slug)}/price-history?${params}`);
    requireResponse(response, `${slug}'s price history`);
    const payload = record(response.payload);
    const cells: HeatmapCell[] = unwrapItems(first(payload.cells, response.payload)).flatMap((entry) => {
      const item = record(entry);
      const weekend = stringOrNull(first(item.weekend, item.weekend_friday));
      const observedOn = stringOrNull(first(item.observed_on, item.observedOn));
      const leadDaysValue = first(item.lead_days, item.leadDays);
      if (!weekend || !observedOn) return [];
      const leadDays = typeof leadDaysValue === 'number'
        ? leadDaysValue
        : Math.max(0, Math.round((new Date(`${weekend}T12:00:00Z`).getTime() - new Date(`${observedOn}T12:00:00Z`).getTime()) / 86_400_000));
      const priceValue = first(item.price_eur_cents, item.price_cents, item.priceCents);
      const priceCents = typeof priceValue === 'number' ? priceValue : null;
      const ruleVersionId = first(item.rule_version_id, item.ruleVersionId);
      const profileId = first(item.profile_id, item.profileId);
      return [{
        weekend,
        observedOn,
        leadDays,
        priceCents,
        availability: normalizeAvailability(item.availability, priceCents !== null),
        coverage: normalizeCoverage(first(item.execution_coverage, item.coverage)),
        sourceScopeRatio: asNumber(first(item.source_scope_ratio, item.sourceScopeRatio)),
        isSynthetic: bool(first(item.is_synthetic, item.isSynthetic)),
        freshness: normalizeFreshness(item.freshness),
        observedAt: stringOrNull(first(item.observed_at, item.observedAt)),
        currentObservedAt: stringOrNull(first(item.current_observed_at, item.currentObservedAt)),
        currentCrawlRunId: asString(first(item.current_crawl_run_id, item.currentCrawlRunId)) || null,
        currentDiagnostic: safeDiagnostic(first(item.current_diagnostic, item.currentDiagnostic)),
        revisionId: asString(first(item.revision_id, item.revisionId)) || null,
        ruleVersionId: typeof ruleVersionId === 'number' ? ruleVersionId : null,
        profileId: typeof profileId === 'number' ? profileId : null,
        priceCompleteness: priceCents === null ? null : normalizePriceCompleteness(first(item.price_completeness, item.priceCompleteness)),
        mandatoryFeesIncluded: priceCents === null
          ? null
          : bool(first(item.mandatory_fees_included, item.mandatoryFeesIncluded), true),
        fareDisclosure: stringOrNull(first(item.fare_disclosure, item.fareDisclosure)),
      }];
    });
    const scale = record(payload.scale);
    const p10 = first(scale.p10_eur_cents, scale.p10_cents, payload.p10_eur_cents);
    const p90 = first(scale.p90_eur_cents, scale.p90_cents, payload.p90_eur_cents);
    return {
      data: { cells, scale: { p10Cents: asNumber(p10, 5_000), p90Cents: asNumber(p90, 25_000) } },
      isSynthetic: cells.some((cell) => cell.isSynthetic),
    };
  },

  async getJourney(slug: string, weekend: string, mode: Mode, revisionId?: string | null): Promise<ApiResult<Journey | null>> {
    const params = new URLSearchParams({ weekend, mode });
    if (revisionId) params.set('revision_id', revisionId);
    const response = await request(`/cities/${encodeURIComponent(slug)}/journeys?${params}`);
    requireResponse(response, `${slug}'s journey details`);
    const payload = record(response.payload);
    const rawJourney = unwrapItems(response.payload)[0];
    const journey = normalizeJourney(rawJourney, payload);
    return journey ? {
        data: journey,
        isSynthetic: journey.isSynthetic,
      } : {
        data: null,
        isSynthetic: false,
        emptyState: {
          availability: normalizeAvailability(payload.availability),
          coverage: normalizeCoverage(first(payload.execution_coverage, payload.coverage)),
          freshness: normalizeFreshness(payload.freshness),
          observedAt: stringOrNull(first(payload.observed_at, payload.observedAt)),
          currentObservedAt: stringOrNull(first(payload.current_observed_at, payload.currentObservedAt)),
          currentCrawlRunId: asString(first(payload.current_crawl_run_id, payload.currentCrawlRunId)) || null,
          currentDiagnostic: safeDiagnostic(first(payload.current_diagnostic, payload.currentDiagnostic)),
        },
      };
  },

  async getLatestBatch(): Promise<ApiResult<Batch | null>> {
    const response = await request('/batches/latest');
    requireResponse(response, 'the latest collection batch');
    const payload = record(response.payload);
    const batchValue = payload.batch ?? response.payload;
    if (batchValue === null) return { data: null };
    const batch = normalizeBatch(batchValue, payload);
    if (!batch) throw new Error('Crowl received an invalid collection batch.');
    return {
      data: batch,
      isSynthetic: /synthetic|fixture/i.test(batch.diagnostic ?? ''),
    };
  },

  async getExtractors(): Promise<ApiResult<Extractor[]>> {
    const response = await request('/extractors');
    requireResponse(response, 'extractor status');
    const extractors = unwrapItems(response.payload).map(normalizeExtractor).filter((item): item is Extractor => item !== null);
    return { data: extractors };
  },
};

const normalizeLeg = (value: unknown, index: number): JourneyLeg | null => {
  const item = record(value);
  const direction = item.direction === 'return' ? 'return' : item.direction === 'outbound' ? 'outbound' : null;
  if (!direction) return null;
  const departsAt = stringOrNull(first(item.departs_at, item.departure_at, item.departsAt));
  const arrivesAt = stringOrNull(first(item.arrives_at, item.arrival_at, item.arrivesAt));
  if (!departsAt || !arrivesAt) return null;
  return {
    id: asString(item.id, `leg-${index}`),
    direction,
    sequence: asNumber(first(item.sequence, item.segment_order, item.order), index + 1),
    mode: normalizeMode(item.mode),
    operator: nestedString(item.operator, 'name', 'label') || 'Operator not provided',
    serviceNumber: stringOrNull(first(item.service_number, item.serviceNumber)),
    originTerminal: nestedString(first(item.origin_terminal, item.originTerminal, item.origin), 'name', 'label') || 'Origin terminal not provided',
    destinationTerminal: nestedString(first(item.destination_terminal, item.destinationTerminal, item.destination), 'name', 'label') || 'Destination terminal not provided',
    departsAt,
    arrivesAt,
    originTimezone: stringOrNull(first(item.origin_timezone, item.originTimezone)),
    destinationTimezone: stringOrNull(first(item.destination_timezone, item.destinationTimezone)),
    durationMinutes: asNumber(first(item.duration_minutes, item.durationMinutes), Math.max(0, Math.round((new Date(arrivesAt).getTime() - new Date(departsAt).getTime()) / 60_000))),
    protection: ['unprotected_transfer', 'unprotected', 'separate_ticket'].includes(asString(first(item.protection, item.protection_state))) ? 'unprotected_transfer' : first(item.protection, item.protection_state) === 'not_applicable' ? 'not_applicable' : 'protected',
  };
};

const normalizeBooking = (value: unknown, index: number): BookingComponent | null => {
  const item = record(value);
  const eurPrice = first(item.price_eur_cents, item.amount_eur_cents, item.eur_amount_cents);
  const price = first(eurPrice, item.price_cents, item.priceCents);
  if (typeof price !== 'number') return null;
  return {
    id: asString(item.id, `booking-${index}`),
    seller: nestedString(item.seller, 'name', 'label') || asString(item.source, 'Seller not provided'),
    source: nestedString(item.source, 'slug', 'name') || 'unknown',
    priceCents: price,
    currency: typeof eurPrice === 'number' ? 'EUR' : asString(item.currency, 'EUR'),
    bookingUrl: stringOrNull(first(item.booking_url, item.bookingUrl)),
    fareName: stringOrNull(first(item.fare_name, item.fareName)),
    studentFare: bool(first(item.student_fare, item.studentFare, item.student_used, item.student_fare_used)),
    priceCompleteness: normalizePriceCompleteness(first(item.price_completeness, item.priceCompleteness)),
    mandatoryFeesIncluded: bool(first(item.mandatory_fees_included, item.mandatoryFeesIncluded)),
    passengerAge: asNumber(first(item.passenger_age, item.passengerAge), 0),
    passengerStudent: bool(first(item.passenger_student, item.passengerStudent)),
    passengerEligibilityConfirmed: bool(first(item.passenger_eligibility_confirmed, item.passengerEligibilityConfirmed)),
    bookableAtObservation: bool(first(item.bookable_at_observation, item.bookableAtObservation)),
    isTeaser: bool(first(item.is_teaser, item.isTeaser)),
    expiresAt: stringOrNull(first(item.expires_at, item.expiresAt)),
    fareDisclosure: stringOrNull(first(item.fare_disclosure, item.fareDisclosure)),
  };
};

const normalizeJourney = (value: unknown, envelope: UnknownRecord): Journey | null => {
  const item = record(value);
  if (!Object.keys(item).length) return null;
  const price = first(item.total_price_eur_cents, item.total_price_cents, item.totalPriceCents);
  if (typeof price !== 'number') return null;
  const legs = (Array.isArray(item.legs) ? item.legs : []).map(normalizeLeg).filter((leg): leg is JourneyLeg => leg !== null);
  const bookings = (Array.isArray(item.bookings) ? item.bookings : []).map(normalizeBooking).filter((booking): booking is BookingComponent => booking !== null);
  const observedAt = stringOrNull(first(item.observed_at, item.observedAt)) ?? new Date().toISOString();
  const currentObservedAt = stringOrNull(first(envelope.current_observed_at, envelope.currentObservedAt));
  const ruleVersionId = first(item.rule_version_id, item.ruleVersionId);
  const profileId = first(item.profile_id, item.profileId);
  return {
    id: asString(first(item.revision_id, item.id), 'journey'),
    ruleVersionId: typeof ruleVersionId === 'number' ? ruleVersionId : null,
    profileId: typeof profileId === 'number' ? profileId : null,
    totalPriceCents: price,
    currency: asString(item.currency, 'EUR'),
    mode: normalizeMode(first(item.mode, envelope.mode, legs[0]?.mode)),
    observedAt,
    currentObservedAt,
    currentCrawlRunId: asString(first(envelope.current_crawl_run_id, envelope.currentCrawlRunId)) || null,
    currentDiagnostic: safeDiagnostic(first(envelope.current_diagnostic, envelope.currentDiagnostic)),
    outboundDepartureAt: asString(first(item.outbound_departure_at, item.outboundDepartureAt, legs.find((leg) => leg.direction === 'outbound')?.departsAt), observedAt),
    returnArrivalAt: asString(first(item.return_arrival_at, item.returnArrivalAt, [...legs].reverse().find((leg) => leg.direction === 'return')?.arrivesAt), observedAt),
    coverage: normalizeCoverage(first(envelope.execution_coverage, envelope.coverage, item.execution_coverage, item.coverage)),
    sourceScopeRatio: asNumber(first(envelope.source_scope_ratio, envelope.sourceScopeRatio, item.source_scope_ratio, item.sourceScopeRatio)),
    isSynthetic: bool(first(item.is_synthetic, item.isSynthetic)),
    freshness: normalizeFreshness(first(item.freshness, envelope.freshness)),
    availability: normalizeAvailability(first(envelope.availability, item.availability), true),
    live: bool(first(item.live, item.is_live), !bool(item.historical)),
    priceCompleteness: normalizePriceCompleteness(first(item.price_completeness, item.priceCompleteness)),
    mandatoryFeesIncluded: bool(first(item.mandatory_fees_included, item.mandatoryFeesIncluded), true),
    fareDisclosure: stringOrNull(first(item.fare_disclosure, item.fareDisclosure)),
    legs,
    bookings,
  };
};

const normalizeBatch = (value: unknown, envelope: UnknownRecord = {}): Batch | null => {
  const item = record(value);
  const id = asString(item.id);
  if (!id) return null;
  const counts = record(item.counts);
  const statusAllowed: BatchStatus[] = ['queued', 'running', 'succeeded', 'partial', 'failed', 'abandoned'];
  const status = statusAllowed.includes(item.status as BatchStatus) ? item.status as BatchStatus : 'failed';
  const state: BatchState = first(item.state, envelope.state) === 'stale' ? 'stale' : 'current';
  return {
    id,
    scheduledFor: asString(first(item.scheduled_for, item.scheduledFor)),
    status,
    state,
    startedAt: stringOrNull(first(item.started_at, item.startedAt)),
    heartbeatAt: stringOrNull(first(item.heartbeat_at, item.heartbeatAt)),
    completedAt: stringOrNull(first(item.completed_at, item.completedAt)),
    publishedAt: stringOrNull(first(item.published_at, item.publishedAt)),
    expectedCells: asNumber(first(item.expected_cells, counts.expected_cells, counts.expected)),
    completedCells: asNumber(first(item.completed_cells, counts.completed_cells, counts.completed)),
    enabledSources: asNumber(first(item.enabled_sources, counts.enabled_sources)),
    knownSources: asNumber(first(item.known_sources, counts.known_sources), 14),
    latestTrustworthyObservationAt: stringOrNull(first(item.latest_trustworthy_observation_at, item.latestTrustworthyObservationAt)),
    nextExpectedAt: stringOrNull(first(item.next_expected_at, item.nextExpectedAt, envelope.next_expected_batch_at, envelope.nextExpectedBatchAt)),
    diagnostic: stringOrNull(first(item.diagnostic, item.diagnostic_summary)),
  };
};

const normalizeExtractor = (value: unknown): Extractor | null => {
  const item = record(value);
  const sourceValue = first(item.source, item.slug);
  const source = nestedString(sourceValue, 'slug', 'id', 'name');
  if (!source) return null;
  const modes = Array.isArray(item.modes) ? item.modes.map((mode) => normalizeMode(mode)) : [normalizeMode(item.mode)];
  const countsValue = record(item.counts);
  const counts: ExtractorCounts = {
    expected: asNumber(first(countsValue.expected, countsValue.expected_searches, item.expected_count)),
    attempted: asNumber(first(countsValue.attempted, countsValue.attempted_searches, item.attempted_count)),
    successful: asNumber(first(countsValue.successful, countsValue.successful_searches, item.successful_count)),
    empty: asNumber(first(countsValue.empty, countsValue.empty_searches, item.empty_count)),
    withResults: asNumber(first(countsValue.with_results, countsValue.result_bearing, item.result_count)),
    physicalRequests: asNumber(first(countsValue.physical_requests, item.physical_request_count)),
  };
  const stateAllowed: ExtractorState[] = ['pending', 'running', 'succeeded', 'partial', 'failed', 'blocked', 'quarantined', 'disabled'];
  const state = stateAllowed.includes(item.state as ExtractorState) ? item.state as ExtractorState : 'failed';
  return {
    source,
    displayName: asString(first(item.display_name, item.displayName, record(sourceValue).display_name), source),
    modes,
    strategy: asString(item.strategy, 'not configured'),
    version: (() => {
      const connector = asString(first(item.version, item.connector_version), 'unknown');
      const parser = stringOrNull(item.parser_version);
      return parser && parser !== connector ? `${connector} · parser ${parser}` : connector;
    })(),
    enabled: bool(item.enabled, state !== 'disabled'),
    disabledReason: stringOrNull(first(item.disabled_reason, item.disabledReason)),
    state,
    lastAttemptAt: stringOrNull(first(item.last_attempt_at, item.lastAttemptAt)),
    lastSuccessAt: stringOrNull(first(item.last_success_at, item.lastSuccessAt)),
    runtimeSeconds: typeof first(item.runtime_seconds, item.runtimeSeconds) === 'number'
      ? first(item.runtime_seconds, item.runtimeSeconds) as number
      : typeof item.runtime_ms === 'number' ? item.runtime_ms / 1_000 : null,
    consecutiveFailures: asNumber(first(item.consecutive_failures, item.consecutiveFailures)),
    counts,
    coverageRatio: asNumber(first(item.coverage_ratio, item.logical_coverage_ratio, item.execution_coverage_ratio), counts.expected ? counts.successful / counts.expected : 0),
    sourceScopeRatio: asNumber(first(item.source_scope_ratio, item.sourceScopeRatio), bool(item.enabled) ? 1 : 0),
    freshness: item.freshness ? normalizeFreshness(item.freshness) : state === 'pending' || state === 'disabled' ? 'never_observed' : state === 'failed' || state === 'blocked' || state === 'quarantined' ? 'stale' : 'fresh',
    diagnostic: stringOrNull(first(item.diagnostic, item.diagnostic_summary)),
  };
};
