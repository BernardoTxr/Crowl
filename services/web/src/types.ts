export type Mode = 'all' | 'flight' | 'train' | 'bus';
export type TravelMode = Exclude<Mode, 'all'> | 'mixed';

export type Availability =
  | 'available'
  | 'no_valid_option'
  | 'not_on_sale'
  | 'sold_out'
  | 'unknown_due_failure';

export type Coverage = 'complete' | 'partial' | 'failed' | 'not_applicable';
export type Freshness = 'fresh' | 'stale' | 'never_observed';
export type PriceCompleteness = 'complete' | 'estimated' | 'unknown';

export interface Coordinates {
  lat: number;
  lon: number;
}

export interface Photo {
  asset: string;
  alt: string;
  creator?: string | null;
  sourceUrl?: string | null;
  license?: string | null;
  objectPosition?: string;
}

export interface City {
  id: string;
  slug: string;
  name: string;
  country: { code: string; name: string };
  timezone: string;
  coordinates: Coordinates;
  status: { visited: boolean; wishlist: boolean };
  categories: string[];
  categoryLabels?: string[];
  summary: string;
  attributes: Record<string, number>;
  attributeLabels?: Record<string, string>;
  photo: Photo;
  typicalDurations?: TypicalDuration[];
}

export interface CatalogDefinition {
  id: string;
  label: string;
  min?: number | null;
  max?: number | null;
  lowLabel?: string | null;
  highLabel?: string | null;
}

export interface CatalogMeta {
  catalogVariant: 'final';
  categoryDefinitions: CatalogDefinition[];
  attributeDefinitions: CatalogDefinition[];
}

export interface TypicalDuration {
  mode: TravelMode;
  medianMinutes: number | null;
  p25Minutes?: number | null;
  p75Minutes?: number | null;
  sampleCount: number;
  isSynthetic?: boolean;
}

export interface Weekend {
  friday: string;
  startsAt: string;
  endsAt: string;
  label: string;
  minimumStayHours: number;
  maxDirectionHours: number;
}

export interface MapFare {
  citySlug: string;
  priceCents: number | null;
  winningMode: TravelMode | null;
  availability: Availability;
  coverage: Coverage;
  sourceScopeRatio: number;
  isSynthetic?: boolean;
  freshness: Freshness;
  /** Original timestamp of the retained trustworthy fare. */
  observedAt: string | null;
  /** Timestamp and batch metadata for the latest collection state. */
  currentObservedAt: string | null;
  currentCrawlRunId: string | null;
  currentDiagnostic: string | null;
  originTerminal: string | null;
  destinationTerminal: string | null;
  returnOriginTerminal?: string | null;
  returnDestinationTerminal?: string | null;
  ruleVersionId?: number | null;
  profileId?: number | null;
  priceCompleteness?: PriceCompleteness | null;
  mandatoryFeesIncluded?: boolean | null;
  fareDisclosure?: string | null;
}

export interface HeatmapCell {
  weekend: string;
  observedOn: string;
  leadDays: number;
  priceCents: number | null;
  availability: Availability;
  coverage: Coverage;
  sourceScopeRatio: number;
  isSynthetic?: boolean;
  freshness: Freshness;
  observedAt: string | null;
  currentObservedAt: string | null;
  currentCrawlRunId: string | null;
  currentDiagnostic: string | null;
  revisionId?: string | null;
  ruleVersionId?: number | null;
  profileId?: number | null;
  priceCompleteness?: PriceCompleteness | null;
  mandatoryFeesIncluded?: boolean | null;
  fareDisclosure?: string | null;
}

export interface PriceHistory {
  cells: HeatmapCell[];
  scale: { p10Cents: number; p90Cents: number };
}

export interface JourneyLeg {
  id: string;
  direction: 'outbound' | 'return';
  sequence: number;
  mode: TravelMode;
  operator: string;
  serviceNumber?: string | null;
  originTerminal: string;
  destinationTerminal: string;
  departsAt: string;
  arrivesAt: string;
  originTimezone?: string | null;
  destinationTimezone?: string | null;
  durationMinutes: number;
  protection: 'protected' | 'unprotected_transfer' | 'not_applicable';
}

export interface BookingComponent {
  id: string;
  seller: string;
  source: string;
  priceCents: number;
  currency: string;
  bookingUrl?: string | null;
  fareName?: string | null;
  studentFare: boolean;
  priceCompleteness?: PriceCompleteness;
  mandatoryFeesIncluded?: boolean;
  passengerAge?: number;
  passengerStudent?: boolean;
  passengerEligibilityConfirmed?: boolean;
  bookableAtObservation?: boolean;
  isTeaser?: boolean;
  expiresAt?: string | null;
  fareDisclosure?: string | null;
}

export interface Journey {
  id: string;
  ruleVersionId?: number | null;
  profileId?: number | null;
  totalPriceCents: number;
  currency: string;
  mode: TravelMode;
  observedAt: string;
  currentObservedAt: string | null;
  currentCrawlRunId: string | null;
  currentDiagnostic: string | null;
  outboundDepartureAt: string;
  returnArrivalAt: string;
  coverage: Coverage;
  sourceScopeRatio: number;
  isSynthetic?: boolean;
  freshness: Freshness;
  availability: Availability;
  live: boolean;
  priceCompleteness?: PriceCompleteness;
  mandatoryFeesIncluded?: boolean;
  fareDisclosure?: string | null;
  legs: JourneyLeg[];
  bookings: BookingComponent[];
}

export interface JourneyEmptyState {
  availability: Availability;
  coverage: Coverage;
  freshness: Freshness;
  observedAt: string | null;
  currentObservedAt: string | null;
  currentCrawlRunId: string | null;
  currentDiagnostic: string | null;
}

export type BatchStatus = 'queued' | 'running' | 'succeeded' | 'partial' | 'failed' | 'abandoned';
export type BatchState = 'current' | 'stale';

export interface Batch {
  id: string;
  scheduledFor: string;
  status: BatchStatus;
  /** Recency of the latest batch, independent of its execution status. */
  state: BatchState;
  startedAt: string | null;
  heartbeatAt: string | null;
  completedAt: string | null;
  publishedAt: string | null;
  expectedCells: number;
  completedCells: number;
  enabledSources: number;
  knownSources: number;
  latestTrustworthyObservationAt: string | null;
  nextExpectedAt: string | null;
  diagnostic: string | null;
}

export type ExtractorState =
  | 'pending'
  | 'running'
  | 'succeeded'
  | 'partial'
  | 'failed'
  | 'blocked'
  | 'quarantined'
  | 'disabled';

export interface ExtractorCounts {
  expected: number;
  attempted: number;
  successful: number;
  empty: number;
  withResults: number;
  physicalRequests: number;
}

export interface Extractor {
  source: string;
  displayName: string;
  modes: TravelMode[];
  strategy: string;
  version: string;
  enabled: boolean;
  disabledReason: string | null;
  state: ExtractorState;
  lastAttemptAt: string | null;
  lastSuccessAt: string | null;
  runtimeSeconds: number | null;
  consecutiveFailures: number;
  counts: ExtractorCounts;
  coverageRatio: number;
  sourceScopeRatio: number;
  freshness: Freshness;
  diagnostic: string | null;
}

export interface ApiResult<T> {
  data: T;
  isSynthetic?: boolean;
  emptyState?: JourneyEmptyState;
}
