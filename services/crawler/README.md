# Crowl crawler

The crawler is a one-shot service. A scheduler starts it, it executes an
immutable task manifest, writes sanitized health events through a storage
abstraction, and exits. It never runs a scheduler or a web server itself.

The fourteen requested sources each have a named module and adapter class under
`crowl_crawler.adapters.providers`. `data/sources.yaml` is the authoritative
catalog for their names, modes, intended extraction strategies, feasibility,
and kill switches; the Python registry does not repeat that metadata. Live
slots are disabled until an authorized distribution channel is configured,
and a catalog kill switch always wins over connector code. The service does
not attempt to evade authentication, rate limits, CAPTCHAs, or bot defenses.
`fixture` is the only adapter enabled by default and is meant for local
development and contract tests.

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
registry, all live providers are intentionally disabled and the job publishes
monitoring state with zero live-source scope; it never substitutes fixture fares:

```sh
export DATABASE_URL=postgresql+psycopg://user:password@host:5432/crowl
crowl-crawler run
```

For every enabled adapter, the production manifest requests both an atomic
round trip and directional outbound/return offers. An adapter may return a
typed unsupported-route result for search kinds it cannot sell. Publication
accepts connector-normalized, complete EUR round trips and independently
bookable one-way offers. Directional offers are composed across enabled
sources and modes by `crowl_core.composition`, then revalidated against the
same weekend, duration, stay, terminal, and Schengen rules before selection.
The two booking components and their respective legs remain separate in SQL.
A provider's bundled round trip is always atomic and is never divided into
directional prices.

Publication is fail-closed at the offer boundary. An advertised fare must be
explicitly complete for mandatory fees, confirmed for the one-adult age-22
student profile (an ordinary adult fare remains valid), bookable rather than a
teaser, and backed by independently bookable components. Non-EUR components
use the latest prior persisted `fx_rates` record; missing, future, invalid, or
older-than-seven-day rates exclude the offer. Original money, the exact EUR
conversion, rate, and rate date remain on `snapshot_bookings`.

Each source's approved booking domains live in `data/sources.yaml`. Only HTTPS
links on those hosts or their subdomains survive publication. User info,
non-standard ports, secret-looking path tokens, fragments, session/auth/cart
parameters, and common tracking parameters are rejected or removed before a
link is stored. The synthetic fixture is separately restricted to
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

- Implement `SourceAdapter` in the existing named provider module after an
  authorized provider feed is available.
- Keep the provider id, modes, strategy, and operational state in
  `data/sources.yaml`; do not duplicate them in the registry.
- Set both `state: enabled` and `enabled: true`, and remove the kill switch,
  only after the connector's access, limits, market coverage, and canary are
  verified. Any active `kill_switch` forces a network-free disabled adapter.
- Produce a `BatchManifest` from the database task ledger and run it with
  `BatchCoordinator`.
- Register an enabled adapter only after its authorized access and credentials
  are ready; `DatabasePublisher` supports both atomic round trips and normalized
  directional offers.

No raw page, credential, cookie, checkout token, or exception message belongs
in a health event. Only allowlisted diagnostic codes are emitted.
