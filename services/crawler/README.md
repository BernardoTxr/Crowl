# Crowl crawler

The crawler is a one-shot service. A scheduler starts it, it executes an
immutable task manifest, writes sanitized health events through a storage
abstraction, and exits. It never runs a scheduler or a web server itself.

The fourteen requested sources each have a named module and adapter class under
`crowl_crawler.adapters.providers`. `data/sources.yaml` is the authoritative
catalog for their names, modes, intended extraction strategies, feasibility,
and kill switches; the Python registry does not repeat that metadata. Sources
can use ordinary public-page HTML, JSON embedded in the page, or a documented
official fare-search interface where the provider permits the planned usage.
Use a live canary to verify the current response.

FlixBus is enabled for direct trips from Paris Bercy Seine to Amsterdam
Sloterdijk and Brussels Gare du Midi. The other 13 live slots remain disabled.
A catalog kill switch applies to the scheduled batch. Ordinary cookie-consent controls may
be accepted, but the service does not log in or evade rate limits, CAPTCHAs,
anti-bot defenses, or other access controls. A difficult or challenged source
is reported and skipped. `fixture` is synthetic and available only through
explicit development/test commands.

Install the shared package and crawler from the repository root:

```sh
python3 -m pip install -e packages/crowl-core -e 'services/crawler[test]'
crowl-crawler sources
crowl-crawler fixture-batch --date 2027-01-08
python3 -m unittest discover -s tests/crawler
```

`fixture-batch` is side-effect free. `fixture-publish` executes all four
synthetic destinations for every eligible weekend in the six-month horizon,
validates each itinerary with `crowl_core.validation`, and publishes the daily
best destination/weekend/mode cells in one database transaction:

```sh
export DATABASE_URL=postgresql+psycopg://user:password@host:5432/crowl
crowl-crawler fixture-publish
```

`live-canary` is the safe first step for checking current provider markup. It
runs one source, one catalog-backed origin/destination terminal pair, and one
Friday weekend. It never opens a database or publishes a fare. Disabled and
kill-switched sources remain network-free unless the invocation includes the
explicit canary-only override:

```sh
export CROWL_RYANAIR_PUBLIC_PAGE_URL='https://www.ryanair.com/gb/en/trip/flights/select'
crowl-crawler live-canary \
  --source ryanair \
  --origin-terminal par-bva \
  --destination-city barcelona-es \
  --destination-terminal bcn-el-prat \
  --weekend 2026-12-04 \
  --override-kill-switch-for-canary \
  --pretty
```

The override exists only in memory for that process. It does not edit
`data/sources.yaml`, enable the Railway batch, discover broad coverage, or
consult `DATABASE_URL`. Output contains normalized prices, journey segments,
typed outcomes, and response/schema fingerprints, but no raw HTML or booking
URL. A successful empty/no-service result exits 0; blocked, parser,
configuration, timeout, or upstream failures exit 1; invalid scope or missing
explicit override exits 2. Provider-specific public-page URL/template and
terminal-alias configuration described below is still required.

The OUIGO adapter uses the official MCP when the catalog selects
`http_structured`. It queries exactly one adult and accepts priced direct
trains on the verified Paris–Brussels and Paris–Lyon pairs. The following
read-only check uses a verified weekend as an example. Select a Friday that
is still on sale when running a new check.

```sh
crowl-crawler live-canary \
  --source ouigo \
  --origin-terminal par-gare-du-nord \
  --destination-city brussels-be \
  --destination-terminal brussels-midi \
  --weekend 2026-12-04 \
  --override-kill-switch-for-canary \
  --pretty
```

Keep the daily OUIGO job disabled until authorized to publish booking links
on Crowl. The dated coverage setting for authorized use is in `.env.example`.
[OUIGO MCP](https://www.ouigo.com/infos-pratiques/mcp-ouigo) ·
[OUIGO sales-site terms](https://www.ouigo.com/infos-legales-site-de-ventes)

`activation-pilot-publish` is the guarded first-write path. It accepts only
FlixBus, requires FlixBus to be the sole enabled provider, runs one exact
round-trip cell, and permanently consumes that operational date's daily slot.
It publishes atomically only when at least one valid winner exists:

```sh
crowl-crawler activation-pilot-publish \
  --source flixbus \
  --origin-terminal par-bercy-seine-bus \
  --destination-city amsterdam-nl \
  --destination-terminal ams-sloterdijk-bus \
  --weekend 2026-11-27 \
  --date 2026-11-21 \
  --acknowledge-daily-slot \
  --pretty
```

The database schema, city catalog, terminal catalog, origin-terminal rules,
and migrations must exist before this command runs. A same-day rerun reuses
the unique `FareSnapshot` cells and skips unchanged revisions. Any validation
or database failure rolls back the run ledger, coverage, health events, fares,
bookings, and legs together.

The `fixture` source is synthetic and never represents one of the 14 requested
providers. Its seed contract is `is_requested=false`, `enabled=false`,
`strategy=fixture`, and connector/parser version `fixture-v1`. The publisher
upserts that row so a production catalog seed may safely omit it.

`run` is the production one-shot command used by Railway. It resolves every
active city, its Schengen terminal links, the complete Paris/Massy origin set,
all fourteen requested source slots, and the rolling six-month weekend horizon
from the database catalog. It runs enabled adapters and persists disabled slots
as explicit `disabled` extractor/search outcomes. With the repository's default
registry, it searches the bounded FlixBus Amsterdam and Brussels coverage and
never substitutes fixture fares:

```sh
export DATABASE_URL=postgresql+psycopg://user:password@host:5432/crowl
crowl-crawler run
```

For every enabled adapter with validated coverage, the production manifest requests both an atomic
round trip and directional outbound/return offers. An adapter may return a
typed unsupported-route result for search kinds it cannot sell. Publication
accepts connector-normalized, complete EUR round trips and independently
bookable one-way offers. Directional offers are composed across enabled
sources and modes by `crowl_core.composition`, then revalidated against the
same weekend, duration, stay, terminal, and Schengen rules before selection.
The two booking components and their respective legs remain separate in SQL.
A provider's bundled round trip is always atomic and is never divided into
directional prices.

A coverage discovery failure is recorded against its source. The same batch
continues searching the other sources with validated coverage. Each adapter's
search timeout starts after it acquires capacity, and its own concurrency
limit controls how many searches can run together. An upstream rate-limit
response stops the source's remaining searches for that batch.

Publication is fail-closed at the offer boundary. An advertised fare must be
explicitly complete for mandatory fees, confirmed for the one-adult age-22
student profile (an ordinary adult fare remains valid), explicitly priced for
one passenger without a paid card, subscription, or membership, bookable
rather than a teaser, and backed by independently bookable components. Non-EUR components
use the latest prior persisted `fx_rates` record; missing, future, invalid, or
older-than-seven-day rates exclude the offer. Original money, the exact EUR
conversion, rate, and rate date remain on `snapshot_bookings`.

The sole exception is an exact FlixBus public result-card amount. It is stored
as `estimated` with mandatory fees not confirmed, may omit the exact-trip
booking URL, and is always exposed as `Displayed fare — service fees may
apply`. Passenger, itinerary, EUR, availability, and paid-entitlement checks
still apply.

Each source's approved booking domains live in `data/sources.yaml`. Only HTTPS
links on those hosts or their subdomains survive publication. User info,
non-standard ports, secret-looking path tokens, fragments, session/auth/cart
parameters, and common tracking parameters are rejected or removed before a
link is stored. A link is optional only for the aligned FlixBus displayed-fare
exception. The synthetic fixture is separately restricted to
`example.invalid`.

Before an enabled adapter is called, `run` commits the immutable daily
`CrawlRun`, pending extractor rows, logical directional coverage, and every
connector-request fingerprint. A PostgreSQL session advisory lock covers the
whole collection and a separate transaction lock serializes publication, so an
overlapping Railway invocation exits safely. SQLite has an in-process
non-blocking fallback for local runs and tests; it cannot coordinate separate
processes.

An interrupted run reuses the same manifest and logical rows. Completed
no-offer outcomes (`success_empty`, `not_on_sale`, `no_service`, `sold_out`,
disabled, and unsupported route) contain everything needed in the durable
ledger and are not requested again. Fare-bearing success and partial-success
payloads are not staged before atomic publication, so they are deliberately
requested again; their prior attempt counts are retained. Pending, transient,
blocked, parse-error, configuration-error, and deadline outcomes are also
retried. A later equal or more expensive same-day result cannot replace the
lowest valid fare already observed. Per-source reports, directional coverage,
requests, and sanitized health events remain visible even when a batch fails.

Build the image from the repository root because it installs both local Python
packages:

```sh
docker build -f services/crawler/Dockerfile -t crowl-crawler .
```

## Extension points

- Use a documented official fare-search interface when its terms permit the
  planned usage. For public HTML, use direct HTTPS when the response contains
  the complete fare, or ordinary Playwright rendering for client-side pages.
- Keep the provider id, modes, strategy, and operational state in
  `data/sources.yaml`; do not duplicate them in the registry.
- Set both `state: enabled` and `enabled: true`, and remove the kill switch,
  only after stable live public markup, exact-fare semantics, terminal aliases,
  route coverage, limits, and canaries are verified. Any active `kill_switch`
  forces a network-free disabled adapter.
- Produce a `BatchManifest` from the database task ledger and run it with
  `BatchCoordinator`.
- `DatabasePublisher` supports both atomic round trips and normalized
  directional offers.

### Public-page configuration

Except for the bounded FlixBus values already present in Compose and Railway,
these variables are intentionally absent from the default environment.
Configure only a source whose live page has been reviewed, then update its
source-catalog state separately:

- Flights: `CROWL_<SOURCE>_PUBLIC_PAGE_URL` and
  `CROWL_<SOURCE>_PUBLIC_ROUTE_PAIRS`. Route pairs are comma-separated canonical
  terminal IDs such as `par-bva:lis-humberto-delgado`.
- Buses: `CROWL_<SOURCE>_PUBLIC_SEARCH_URL_TEMPLATE`,
  `CROWL_<SOURCE>_TERMINAL_ALIASES_JSON`, and
  `CROWL_<SOURCE>_COVERAGE_JSON`. URL templates accept `{origin}`,
  `{destination}`, `{date}`, `{date_dot}`, and `{direction}`.
- Trains: `CROWL_<SOURCE>_HTML_URL_TEMPLATE`,
  `CROWL_<SOURCE>_TERMINAL_ALIASES_JSON`, and
  `CROWL_<SOURCE>_COVERAGE_JSON`; the optional
  `CROWL_<SOURCE>_TIMEOUT_SECONDS` is bounded to 1–60 seconds. URL templates
  accept provider terminal aliases through `{origin}` and `{destination}`, plus
  dates, direction, search kind, passenger, and currency fields documented in
  `rail_html.py`.

FlixBus needs two deliberately separate alias sets. Its
`CROWL_FLIXBUS_TERMINAL_ALIASES_JSON` maps Crowl terminal IDs to the provider's
city UUIDs used only to form the public search URL. Its
`CROWL_FLIXBUS_STOP_ALIASES_JSON` maps each exact stop label rendered in a
result card to a canonical Crowl terminal ID, for example
`{"Paris (Bercy Seine)":"par-bercy-seine-bus","Amsterdam Sloterdijk":"ams-sloterdijk-bus"}`.
Unmapped result stops are never inferred from the city UUID. Direct cards with
exact aliases, valid weekend times, and the configured passenger/currency are
normalized as displayed fares; connected cards remain diagnostic-only.

Configuration alone is not an enablement signal. `data/sources.yaml` enables
only FlixBus for the two verified routes; the other 13 entries remain disabled
and kill-switched until their own live-page verification is complete.

No raw page, credential, cookie, checkout token, or exception message belongs
in a health event. Only allowlisted diagnostic codes are emitted.
