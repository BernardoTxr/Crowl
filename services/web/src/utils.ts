import type { Availability, BatchStatus, Coverage, Freshness, Mode, TravelMode, Weekend } from './types';

export const FLIXBUS_DISPLAYED_FARE_DISCLOSURE = 'Displayed fare — service fees may apply';

export const MODES: Array<{ value: Mode; label: string; icon: string }> = [
  { value: 'all', label: 'Any', icon: '◎' },
  { value: 'flight', label: 'Flight', icon: '✈' },
  { value: 'train', label: 'Train', icon: '▰' },
  { value: 'bus', label: 'Bus', icon: '▣' },
];

export const modeLabel = (mode: TravelMode | null | undefined) =>
  mode === 'mixed' ? 'Mixed' : MODES.find((item) => item.value === mode)?.label ?? 'Unknown';

export const formatCurrency = (cents: number | null | undefined, currency = 'EUR') => {
  if (cents === null || cents === undefined) return '—';
  return new Intl.NumberFormat('en-GB', {
    style: 'currency',
    currency,
    maximumFractionDigits: cents % 100 === 0 ? 0 : 2,
  }).format(cents / 100);
};

export const formatDate = (value: string, options?: Intl.DateTimeFormatOptions) =>
  new Intl.DateTimeFormat('en-GB', {
    day: 'numeric',
    month: 'short',
    year: 'numeric',
    ...options,
  }).format(new Date(value));

export const formatDateTime = (value: string | null | undefined, timeZone?: string | null) => {
  if (!value) return 'Not available';
  return new Intl.DateTimeFormat('en-GB', {
    day: 'numeric',
    month: 'short',
    hour: '2-digit',
    minute: '2-digit',
    timeZoneName: 'short',
    ...(timeZone ? { timeZone } : {}),
  }).format(new Date(value));
};

export const formatDuration = (minutes: number | null | undefined) => {
  if (minutes === null || minutes === undefined) return 'Not enough data';
  const hours = Math.floor(minutes / 60);
  const remainder = minutes % 60;
  return remainder ? `${hours}h ${remainder}m` : `${hours}h`;
};

export const availabilityLabel: Record<Availability, string> = {
  available: 'Available',
  no_valid_option: 'No eligible result',
  not_on_sale: 'Not on sale yet',
  sold_out: 'Sold out',
  unknown_due_failure: 'Collection failed',
};

export const coverageLabel: Record<Coverage, string> = {
  complete: 'Complete coverage',
  partial: 'Partial coverage',
  failed: 'Coverage failed',
  not_applicable: 'Not applicable',
};

export const freshnessLabel: Record<Freshness, string> = {
  fresh: 'Fresh',
  stale: 'Stale',
  never_observed: 'Never observed',
};

export const batchLabel: Record<BatchStatus, string> = {
  queued: 'Queued',
  running: 'Collecting',
  succeeded: 'Healthy',
  partial: 'Degraded',
  failed: 'Failed',
  abandoned: 'Abandoned',
};

export const priceBand = (priceCents: number | null) => {
  if (priceCents === null) return 'price-none';
  if (priceCents < 5_000) return 'price-1';
  if (priceCents < 10_000) return 'price-2';
  if (priceCents < 17_500) return 'price-3';
  if (priceCents < 25_000) return 'price-4';
  return 'price-5';
};

export const safeMode = (value: string | null): Mode =>
  value === 'flight' || value === 'train' || value === 'bus' ? value : 'all';

export const nextNonpastWeekend = (weekends: Weekend[], now = Date.now()) => {
  const ordered = [...weekends].sort((a, b) => Date.parse(a.startsAt) - Date.parse(b.startsAt));
  return ordered.find((weekend) => {
    const end = Date.parse(weekend.endsAt);
    return Number.isFinite(end) ? end >= now : Date.parse(`${weekend.friday}T23:59:59Z`) >= now;
  })?.friday ?? ordered[0]?.friday;
};

export const joinClassNames = (...items: Array<string | false | null | undefined>) =>
  items.filter(Boolean).join(' ');

export const categoryLabel = (value: string) =>
  (value === 'expense' ? 'expensive' : value)
    .replace(/[-_]/g, ' ')
    .replace(/\b\w/g, (character) => character.toUpperCase());

export const asNumber = (value: unknown, fallback = 0) =>
  typeof value === 'number' && Number.isFinite(value) ? value : fallback;

export const asString = (value: unknown, fallback = '') =>
  typeof value === 'string' ? value : typeof value === 'number' && Number.isFinite(value) ? String(value) : fallback;
