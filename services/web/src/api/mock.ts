import type {
  Availability,
  Batch,
  CatalogMeta,
  City,
  Extractor,
  HeatmapCell,
  Journey,
  MapFare,
  Mode,
  PriceHistory,
  TravelMode,
  Weekend,
} from '../types';

export const mockCatalogMeta: CatalogMeta = {
  catalogVariant: 'mock',
  categoryDefinitions: [
    'culture', 'food', 'nightlife', 'winter', 'adventure', 'beach', 'nature', 'architecture',
  ].map((id) => ({ id, label: id[0].toUpperCase() + id.slice(1) })),
  attributeDefinitions: [
    { id: 'expense', label: 'Expensive', min: 1, max: 5, lowLabel: 'Affordable', highLabel: 'Very expensive' },
    { id: 'culture', label: 'Culture', min: 1, max: 5, lowLabel: 'Limited', highLabel: 'Exceptional' },
    { id: 'nightlife', label: 'Nightlife', min: 1, max: 5, lowLabel: 'Quiet', highLabel: 'Very active' },
    { id: 'nature', label: 'Nature', min: 1, max: 5, lowLabel: 'Limited', highLabel: 'Exceptional' },
    { id: 'food', label: 'Food', min: 1, max: 5, lowLabel: 'Limited', highLabel: 'Exceptional' },
  ],
};

const photo = (asset: string, alt: string, creator: string, sourceUrl: string) => ({
  asset: `${asset}?auto=format&fit=crop&w=1600&q=82`,
  alt,
  creator,
  sourceUrl,
  license: 'Unsplash License',
  objectPosition: '50% 50%',
});

export const mockCities: City[] = [
  {
    id: 'amsterdam-nl', slug: 'amsterdam', name: 'Amsterdam',
    country: { code: 'NL', name: 'Netherlands' }, timezone: 'Europe/Amsterdam',
    coordinates: { lat: 52.3676, lon: 4.9041 }, status: { visited: true, wishlist: false },
    categories: ['culture', 'nightlife'], summary: 'Canals, compact neighbourhoods, museums, and an easy rail arrival.',
    attributes: { expense: 4, culture: 5, nightlife: 4, nature: 2, food: 4 },
    photo: photo('https://images.unsplash.com/photo-1534351590666-13e3e96b5017', 'Canal houses reflected in Amsterdam water', 'Adrien Olichon', 'https://unsplash.com/photos/2YTQGEdv-lE'),
  },
  {
    id: 'lisbon-pt', slug: 'lisbon', name: 'Lisbon',
    country: { code: 'PT', name: 'Portugal' }, timezone: 'Europe/Lisbon',
    coordinates: { lat: 38.7223, lon: -9.1393 }, status: { visited: false, wishlist: true },
    categories: ['culture', 'food', 'beach'], summary: 'Hillside neighbourhoods, Atlantic light, tiled streets, and nearby beaches.',
    attributes: { expense: 3, culture: 5, nightlife: 4, nature: 3, food: 5 },
    photo: photo('https://images.unsplash.com/photo-1555881400-74d7acaacd8b', 'Lisbon rooftops and the Tagus in warm evening light', 'Liam McKay', 'https://unsplash.com/photos/CPq4b6b0VJ8'),
  },
  {
    id: 'vienna-at', slug: 'vienna', name: 'Vienna',
    country: { code: 'AT', name: 'Austria' }, timezone: 'Europe/Vienna',
    coordinates: { lat: 48.2082, lon: 16.3738 }, status: { visited: false, wishlist: true },
    categories: ['culture', 'food', 'winter'], summary: 'Imperial architecture, serious museums, tiny cafés, and winter markets.',
    attributes: { expense: 4, culture: 5, nightlife: 3, nature: 3, food: 4 },
    photo: photo('https://images.unsplash.com/photo-1516550893923-42d28e5677af', 'Historic Vienna architecture at dusk', 'Jacek Dylag', 'https://unsplash.com/photos/5nbF03IYwLM'),
  },
  {
    id: 'reykjavik-is', slug: 'reykjavik', name: 'Reykjavík',
    country: { code: 'IS', name: 'Iceland' }, timezone: 'Atlantic/Reykjavik',
    coordinates: { lat: 64.1466, lon: -21.9426 }, status: { visited: false, wishlist: true },
    categories: ['winter', 'adventure', 'nature'], summary: 'A compact base for geothermal landscapes, long light, and winter skies.',
    attributes: { expense: 5, culture: 3, nightlife: 3, nature: 5, food: 3 },
    photo: photo('https://images.unsplash.com/photo-1504893524553-b855bce32c67', 'Icelandic mountain road and winter landscape', 'Norris Niman', 'https://unsplash.com/photos/ABtmE3jhaPQ'),
  },
  {
    id: 'barcelona-es', slug: 'barcelona', name: 'Barcelona',
    country: { code: 'ES', name: 'Spain' }, timezone: 'Europe/Madrid',
    coordinates: { lat: 41.3874, lon: 2.1686 }, status: { visited: true, wishlist: false },
    categories: ['culture', 'food', 'beach'], summary: 'Bold architecture, late dinners, lively streets, and the Mediterranean edge.',
    attributes: { expense: 3, culture: 5, nightlife: 5, nature: 3, food: 5 },
    photo: photo('https://images.unsplash.com/photo-1539037116277-4db20889f2d4', 'Barcelona rooftops with the Sagrada Família', 'Vitor Monteiro', 'https://unsplash.com/photos/qpZfC4Z3JH8'),
  },
  {
    id: 'prague-cz', slug: 'prague', name: 'Prague',
    country: { code: 'CZ', name: 'Czechia' }, timezone: 'Europe/Prague',
    coordinates: { lat: 50.0755, lon: 14.4378 }, status: { visited: false, wishlist: true },
    categories: ['culture', 'nightlife', 'winter'], summary: 'River walks, layered architecture, neighbourhood pubs, and compact old streets.',
    attributes: { expense: 2, culture: 5, nightlife: 4, nature: 2, food: 4 },
    photo: photo('https://images.unsplash.com/photo-1541849546-216549ae216d', 'Prague skyline and bridges across the Vltava', 'Rodrigo Ardilha', 'https://unsplash.com/photos/o2b4GQnQ3YI'),
  },
  {
    id: 'rome-it', slug: 'rome', name: 'Rome',
    country: { code: 'IT', name: 'Italy' }, timezone: 'Europe/Rome',
    coordinates: { lat: 41.9028, lon: 12.4964 }, status: { visited: true, wishlist: true },
    categories: ['culture', 'food'], summary: 'Ancient layers, neighbourhood trattorias, generous piazzas, and endless detail.',
    attributes: { expense: 4, culture: 5, nightlife: 3, nature: 2, food: 5 },
    photo: photo('https://images.unsplash.com/photo-1529260830199-42c24126f198', 'Roman rooftops around a historic dome', 'Christopher Czermak', 'https://unsplash.com/photos/7ybKmhDTcz0'),
  },
  {
    id: 'copenhagen-dk', slug: 'copenhagen', name: 'Copenhagen',
    country: { code: 'DK', name: 'Denmark' }, timezone: 'Europe/Copenhagen',
    coordinates: { lat: 55.6761, lon: 12.5683 }, status: { visited: false, wishlist: true },
    categories: ['food', 'culture', 'winter'], summary: 'Harbour life, thoughtful design, exceptional food, and bicycle-first streets.',
    attributes: { expense: 5, culture: 4, nightlife: 3, nature: 3, food: 5 },
    photo: photo('https://images.unsplash.com/photo-1513622470522-26c3c8a854bc', 'Colourful harbour houses in Copenhagen', 'Nick Karvounis', 'https://unsplash.com/photos/3_ZGrsirryY'),
  },
  {
    id: 'berlin-de', slug: 'berlin', name: 'Berlin',
    country: { code: 'DE', name: 'Germany' }, timezone: 'Europe/Berlin',
    coordinates: { lat: 52.52, lon: 13.405 }, status: { visited: true, wishlist: false },
    categories: ['culture', 'nightlife'], summary: 'Contemporary art, layered history, club culture, and sprawling green corners.',
    attributes: { expense: 3, culture: 5, nightlife: 5, nature: 3, food: 4 },
    photo: photo('https://images.unsplash.com/photo-1560969184-10fe8719e047', 'Berlin cityscape around the television tower', 'Florian Wehde', 'https://unsplash.com/photos/uFGi0_YciE0'),
  },
  {
    id: 'zurich-ch', slug: 'zurich', name: 'Zürich',
    country: { code: 'CH', name: 'Switzerland' }, timezone: 'Europe/Zurich',
    coordinates: { lat: 47.3769, lon: 8.5417 }, status: { visited: false, wishlist: true },
    categories: ['nature', 'adventure', 'winter'], summary: 'A polished lakeside city with quick access to trails, water, and mountain air.',
    attributes: { expense: 5, culture: 4, nightlife: 3, nature: 5, food: 4 },
    photo: photo('https://images.unsplash.com/photo-1527668752968-14dc70a27c95', 'Zürich old town beside the river', 'Patrick Federi', 'https://unsplash.com/photos/hNNvULJFZkE'),
  },
  {
    id: 'stockholm-se', slug: 'stockholm', name: 'Stockholm',
    country: { code: 'SE', name: 'Sweden' }, timezone: 'Europe/Stockholm',
    coordinates: { lat: 59.3293, lon: 18.0686 }, status: { visited: false, wishlist: true },
    categories: ['culture', 'nature', 'winter'], summary: 'Island neighbourhoods, ferries, cinnamon buns, and an easy relationship with water.',
    attributes: { expense: 5, culture: 4, nightlife: 3, nature: 5, food: 4 },
    photo: photo('https://images.unsplash.com/photo-1509356843151-3e7d96241e11', 'Stockholm waterfront under a clear sky', 'Joseph Richard Francis', 'https://unsplash.com/photos/NqV9G5GgP38'),
  },
  {
    id: 'krakow-pl', slug: 'krakow', name: 'Kraków',
    country: { code: 'PL', name: 'Poland' }, timezone: 'Europe/Warsaw',
    coordinates: { lat: 50.0647, lon: 19.945 }, status: { visited: false, wishlist: true },
    categories: ['culture', 'food', 'winter'], summary: 'A walkable old centre, deep history, cafés, and excellent value for a weekend.',
    attributes: { expense: 2, culture: 5, nightlife: 4, nature: 3, food: 4 },
    photo: photo('https://images.unsplash.com/photo-1519197924294-4ba991a11128', 'Kraków main square and historic towers', 'Jacek Dylag', 'https://unsplash.com/photos/7Xc7JHThB2M'),
  },
];

mockCities.forEach((city, index) => {
  city.typicalDurations = [
    { mode: 'flight', medianMinutes: 115 + index * 9, p25Minutes: 105 + index * 8, p75Minutes: 135 + index * 10, sampleCount: 28 + index * 3 },
    { mode: 'train', medianMinutes: index < 2 || [4, 8, 9].includes(index) ? 205 + index * 31 : null, p25Minutes: null, p75Minutes: null, sampleCount: index < 2 || [4, 8, 9].includes(index) ? 15 + index : 0 },
    { mode: 'bus', medianMinutes: index < 10 ? 430 + index * 27 : null, p25Minutes: null, p75Minutes: null, sampleCount: index < 10 ? 12 + index : 0 },
  ];
});

const isoDate = (date: Date) => date.toISOString().slice(0, 10);

export const makeMockWeekends = (count = 27): Weekend[] => {
  const cursor = new Date();
  cursor.setUTCHours(12, 0, 0, 0);
  const daysUntilFriday = (5 - cursor.getUTCDay() + 7) % 7;
  cursor.setUTCDate(cursor.getUTCDate() + daysUntilFriday);
  if (daysUntilFriday === 0 && new Date().getHours() >= 19) cursor.setUTCDate(cursor.getUTCDate() + 7);

  return Array.from({ length: count }, (_, index) => {
    const friday = new Date(cursor);
    friday.setUTCDate(friday.getUTCDate() + index * 7);
    const monday = new Date(friday);
    monday.setUTCDate(monday.getUTCDate() + 3);
    const label = `${new Intl.DateTimeFormat('en-GB', { day: 'numeric', month: 'short' }).format(friday)} — ${new Intl.DateTimeFormat('en-GB', { day: 'numeric', month: 'short' }).format(monday)}`;
    return {
      friday: isoDate(friday),
      startsAt: `${isoDate(friday)}T19:00:00+01:00`,
      endsAt: `${isoDate(monday)}T07:00:00+01:00`,
      label,
      minimumStayHours: 12,
      maxDirectionHours: 12,
    };
  });
};

const hash = (value: string) => Array.from(value).reduce((total, character) => ((total * 31) + character.charCodeAt(0)) % 997, 17);

const cityMode = (city: City, mode: Mode): TravelMode => {
  if (mode !== 'all') return mode;
  if (['amsterdam', 'brussels', 'zurich'].includes(city.slug)) return 'train';
  if (city.slug === 'berlin') return 'mixed';
  return 'flight';
};

const basePrice = (city: City, mode: TravelMode) => {
  const cityIndex = mockCities.findIndex((item) => item.slug === city.slug);
  const modeAdjustment = { flight: 0, train: 3_500, bus: -1_200, mixed: 1_500 }[mode];
  return Math.max(2_900, 4_200 + cityIndex * 1_070 + modeAdjustment);
};

const terminals: Record<string, [string, string]> = {
  amsterdam: ['Paris Gare du Nord', 'Amsterdam Centraal'],
  lisbon: ['Paris Orly', 'Lisbon Humberto Delgado Airport'],
  vienna: ['Paris Charles de Gaulle', 'Vienna International Airport'],
  reykjavik: ['Paris Orly', 'Keflavík International Airport'],
  barcelona: ['Paris Orly', 'Barcelona–El Prat Airport'],
  prague: ['Paris Charles de Gaulle', 'Václav Havel Airport Prague'],
  rome: ['Paris Orly', 'Rome Fiumicino Airport'],
  copenhagen: ['Paris Charles de Gaulle', 'Copenhagen Airport'],
  berlin: ['Paris Gare de l’Est', 'Berlin Hauptbahnhof'],
  zurich: ['Paris Gare de Lyon', 'Zürich Hauptbahnhof'],
  stockholm: ['Paris Charles de Gaulle', 'Stockholm Arlanda Airport'],
  krakow: ['Paris Orly', 'Kraków John Paul II Airport'],
};

export const makeMockMapFares = (weekend: string, mode: Mode): MapFare[] =>
  mockCities.map((city, index) => {
    const selectedMode = cityMode(city, mode);
    const stateSeed = hash(`${weekend}-${city.slug}-${mode}`);
    let availability: Availability = 'available';
    if (stateSeed % 29 === 0) availability = 'not_on_sale';
    else if (stateSeed % 23 === 0) availability = 'unknown_due_failure';
    else if (stateSeed % 19 === 0) availability = 'no_valid_option';
    const price = availability === 'available'
      ? basePrice(city, selectedMode) + ((stateSeed % 42) * 100)
      : null;
    const [origin, destination] = terminals[city.slug] ?? ['Paris', city.name];
    return {
      citySlug: city.slug,
      priceCents: price,
      winningMode: availability === 'available' ? selectedMode : null,
      availability,
      coverage: index % 7 === 0 ? 'partial' : availability === 'unknown_due_failure' ? 'failed' : 'complete',
      sourceScopeRatio: index % 7 === 0 ? 0.71 : 0.86,
      freshness: index === 9 ? 'stale' : 'fresh',
      observedAt: new Date(Date.now() - (index === 9 ? 28 : 3) * 60 * 60 * 1000).toISOString(),
      currentObservedAt: new Date(Date.now() - (index === 9 ? 28 : 3) * 60 * 60 * 1000).toISOString(),
      currentCrawlRunId: `mock-map-${weekend}`,
      currentDiagnostic: availability === 'unknown_due_failure' ? 'MOCK_COLLECTION_FAILED' : null,
      originTerminal: origin,
      destinationTerminal: destination,
    };
  });

const dateWithTime = (date: string, dayOffset: number, hour: number, minute = 0) => {
  const value = new Date(`${date}T12:00:00Z`);
  value.setUTCDate(value.getUTCDate() + dayOffset);
  value.setUTCHours(hour, minute, 0, 0);
  return value.toISOString();
};

export const makeMockHistory = (city: City, mode: Mode): PriceHistory => {
  const selectedMode = cityMode(city, mode);
  const weekends = makeMockWeekends(16);
  const leadDays = [180, 150, 120, 90, 60, 30, 14, 7, 0];
  const cells: HeatmapCell[] = [];
  weekends.forEach((weekend, row) => {
    leadDays.forEach((leadDay, column) => {
      const scheduledDate = new Date(`${weekend.friday}T12:00:00Z`);
      scheduledDate.setUTCDate(scheduledDate.getUTCDate() - leadDay);
      const notHappened = scheduledDate.getTime() > Date.now();
      const notOnSale = leadDay >= 150 && (row + column) % 4 === 0;
      const failed = (row * 9 + column) % 31 === 0;
      const availability: Availability = notHappened || notOnSale
        ? 'not_on_sale'
        : failed
          ? 'unknown_due_failure'
          : 'available';
      const approachPremium = Math.max(0, 45 - leadDay) * 90;
      cells.push({
        weekend: weekend.friday,
        observedOn: isoDate(scheduledDate),
        leadDays: leadDay,
        priceCents: availability === 'available'
          ? basePrice(city, selectedMode) + approachPremium + ((hash(`${city.slug}-${row}-${column}`) % 28) * 100)
          : null,
        availability,
        coverage: failed ? 'failed' : (row + column) % 13 === 0 ? 'partial' : 'complete',
        sourceScopeRatio: 0.79,
        freshness: 'fresh',
        observedAt: availability === 'available' ? scheduledDate.toISOString() : null,
        currentObservedAt: scheduledDate.toISOString(),
        currentCrawlRunId: `mock-history-${isoDate(scheduledDate)}`,
        currentDiagnostic: failed ? 'MOCK_COLLECTION_FAILED' : null,
        revisionId: availability === 'available' ? `mock-${city.slug}-${row}-${column}` : null,
      });
    });
  });
  const prices = cells.flatMap((cell) => cell.priceCents === null ? [] : [cell.priceCents]).sort((a, b) => a - b);
  return {
    cells,
    scale: {
      p10Cents: prices[Math.floor(prices.length * 0.1)] ?? 5_000,
      p90Cents: prices[Math.floor(prices.length * 0.9)] ?? 25_000,
    },
  };
};

const operatorFor = (mode: TravelMode) => ({
  flight: 'Transavia', train: 'SNCF Connect', bus: 'FlixBus', mixed: 'SNCF + easyJet',
}[mode]);

export const makeMockJourney = (city: City, weekend: string, mode: Mode): Journey => {
  const selectedMode = cityMode(city, mode);
  const [origin, destination] = terminals[city.slug] ?? ['Paris', city.name];
  const hasSeparateTickets = selectedMode === 'mixed';
  const duration = selectedMode === 'bus' ? 590 : selectedMode === 'train' ? 260 : 145;
  const observedAt = new Date(Date.now() - 3 * 60 * 60 * 1000).toISOString();
  const outboundDepartureAt = dateWithTime(weekend, 0, 20, 10);
  const outboundArrivalAt = new Date(new Date(outboundDepartureAt).getTime() + duration * 60_000).toISOString();
  const returnDepartureAt = dateWithTime(weekend, 2, 18, 15);
  const returnArrivalAt = new Date(new Date(returnDepartureAt).getTime() + duration * 60_000).toISOString();
  const operator = operatorFor(selectedMode);
  const total = basePrice(city, selectedMode) + ((hash(`${city.slug}-${weekend}`) % 35) * 100);
  return {
    id: `mock-journey-${city.slug}-${weekend}-${selectedMode}`,
    totalPriceCents: total,
    currency: 'EUR',
    mode: selectedMode,
    observedAt,
    currentObservedAt: observedAt,
    currentCrawlRunId: `mock-journey-${weekend}`,
    currentDiagnostic: null,
    outboundDepartureAt,
    returnArrivalAt,
    coverage: city.slug === 'reykjavik' ? 'partial' : 'complete',
    sourceScopeRatio: 0.64,
    freshness: 'fresh',
    availability: 'available',
    live: true,
    legs: [
      { id: 'outbound-1', direction: 'outbound', sequence: 1, mode: selectedMode, operator, serviceNumber: selectedMode === 'flight' ? 'TO 4020' : 'Weekend service', originTerminal: origin, destinationTerminal: destination, departsAt: outboundDepartureAt, arrivesAt: outboundArrivalAt, durationMinutes: duration, protection: hasSeparateTickets ? 'unprotected_transfer' : 'protected' },
      { id: 'return-1', direction: 'return', sequence: 1, mode: selectedMode, operator, serviceNumber: selectedMode === 'flight' ? 'TO 4021' : 'Weekend service', originTerminal: destination, destinationTerminal: origin, departsAt: returnDepartureAt, arrivesAt: returnArrivalAt, durationMinutes: duration, protection: hasSeparateTickets ? 'unprotected_transfer' : 'protected' },
    ],
    bookings: hasSeparateTickets
      ? [
          { id: 'booking-1', seller: 'SNCF Connect', source: 'sncf-connect', priceCents: Math.floor(total * 0.44), currency: 'EUR', bookingUrl: null, fareName: 'Standard', studentFare: true },
          { id: 'booking-2', seller: 'easyJet', source: 'easyjet', priceCents: total - Math.floor(total * 0.44), currency: 'EUR', bookingUrl: null, fareName: 'Basic', studentFare: false },
        ]
      : [{ id: 'booking-1', seller: operator, source: operator.toLowerCase().replaceAll(' ', '-'), priceCents: total, currency: 'EUR', bookingUrl: null, fareName: selectedMode === 'flight' ? 'Basic' : 'Standard', studentFare: selectedMode === 'train' }],
  };
};

export const mockBatch: Batch = {
  id: 'demo-batch',
  scheduledFor: isoDate(new Date()),
  status: 'partial',
  state: 'current',
  startedAt: new Date(Date.now() - 6.4 * 60 * 60 * 1000).toISOString(),
  heartbeatAt: new Date(Date.now() - 35 * 60 * 1000).toISOString(),
  completedAt: new Date(Date.now() - 42 * 60 * 1000).toISOString(),
  publishedAt: new Date(Date.now() - 40 * 60 * 1000).toISOString(),
  expectedCells: 12_480,
  completedCells: 10_904,
  enabledSources: 9,
  knownSources: 14,
  latestTrustworthyObservationAt: new Date(Date.now() - 45 * 60 * 1000).toISOString(),
  nextExpectedAt: new Date(Date.now() + 18 * 60 * 60 * 1000).toISOString(),
  diagnostic: 'Synthetic mock batch',
};

const sources: Array<[string, string, TravelMode, string]> = [
  ['ryanair', 'Ryanair', 'flight', 'http_structured'],
  ['easyjet', 'easyJet', 'flight', 'http_structured'],
  ['transavia', 'Transavia', 'flight', 'http_structured'],
  ['wizzair', 'Wizz Air', 'flight', 'http_structured'],
  ['vueling', 'Vueling', 'flight', 'http_structured'],
  ['volotea', 'Volotea', 'flight', 'playwright'],
  ['flixbus', 'FlixBus', 'bus', 'http_structured'],
  ['blablacar-bus', 'BlaBlaCar Bus', 'bus', 'http_html'],
  ['itabus', 'Itabus', 'bus', 'http_html'],
  ['alsa', 'Alsa', 'bus', 'playwright'],
  ['ouigo', 'OUIGO', 'train', 'http_structured'],
  ['sncf-connect', 'SNCF Connect', 'train', 'http_structured'],
  ['trenitalia-france', 'Trenitalia France', 'train', 'http_structured'],
  ['eurostar', 'Eurostar', 'train', 'http_structured'],
];

export const mockExtractors: Extractor[] = sources.map(([source, displayName, mode, strategy], index) => {
  const disabled = index === 5 || index === 8;
  const blocked = index === 3;
  const partial = index === 7 || index === 12;
  const state = disabled ? 'disabled' : blocked ? 'blocked' : partial ? 'partial' : 'succeeded';
  const expected = 820 + index * 23;
  const attempted = disabled ? 0 : blocked ? 14 : expected;
  const successful = disabled ? 0 : blocked ? 0 : partial ? Math.floor(attempted * 0.78) : attempted;
  return {
    source, displayName, modes: [mode], strategy, version: 'v1.0.0', enabled: !disabled,
    disabledReason: disabled ? 'Connector access is not configured.' : null,
    state,
    lastAttemptAt: disabled ? null : new Date(Date.now() - (index + 1) * 12 * 60 * 1000).toISOString(),
    lastSuccessAt: blocked ? new Date(Date.now() - 3 * 24 * 60 * 60 * 1000).toISOString() : disabled ? null : new Date(Date.now() - (index + 1) * 15 * 60 * 1000).toISOString(),
    runtimeSeconds: disabled ? null : 95 + index * 17,
    consecutiveFailures: blocked ? 3 : partial ? 1 : 0,
    counts: { expected, attempted, successful, empty: Math.floor(successful * 0.18), withResults: Math.floor(successful * 0.82), physicalRequests: Math.ceil(attempted * 0.42) },
    coverageRatio: expected ? successful / expected : 0,
    sourceScopeRatio: disabled ? 0 : 1,
    freshness: blocked ? 'stale' : 'fresh',
    diagnostic: disabled ? 'Waiting for a supported connector configuration.' : blocked ? 'Source challenge detected; results were not published.' : partial ? 'Some searches timed out; available results remain trustworthy.' : 'Completed within the expected range.',
  } as Extractor;
});
