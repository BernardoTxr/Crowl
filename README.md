# Crowl

Crowl (crow + crawl) is a private, read-only weekend travel monitor for the
Schengen area. It compares complete round-trip prices from Paris-area
terminals, retains daily price history, and presents destinations on an
interactive map and city pages.

This repository contains a runnable local v1 built around a 12-city mock
catalog and clearly labeled synthetic fares. It is not a live-fare service
yet: the final, manually curated JSON of approximately 150 cities is still
required, and all 14 requested provider connectors remain disabled until an
authorized feed, credentials, or distribution access is available.

The current overview is an interactive, stylized SVG map with city markers,
pan/zoom, clustering, keyboard controls, and an equivalent city list. A real
geographic tile layer and Schengen boundary/mask are still pending. Current
cross-source composition can pair outbound and return components from
different sources; it does not yet build one direction from arbitrary legs
published by different sources.

For a precise separation between what works now and what still depends on
external input, see [docs/implementation-status.md](docs/implementation-status.md).

The detailed product and data model are in [plan.md](plan.md). Railway setup,
private networking, migrations, and cron behavior are in
[docs/deployment.md](docs/deployment.md).

## Travel rules

Crowl applies the following rules before a fare can be published:

- One traveler, age 22; use a genuinely available student/youth fare or the
  ordinary adult fare. Prices are in EUR and include mandatory taxes and fees
  plus the smallest included personal item, with no paid baggage or membership.
- Outbound travel starts at an allowed Paris-area airport, passenger rail/bus
  terminal, or Massy. Return may use a different Paris terminal. Travel from
  Palaiseau to that terminal is excluded from duration and price.
- The first outbound departure is no earlier than Friday 19:00 Paris time and
  final arrival back in Paris is no later than Monday 07:00 Paris time.
- Each direction is at most 12 hours terminal-to-terminal, including connection
  waits, and the destination stay is at least 12 hours.
- The horizon is the next six calendar months. Public holidays receive no
  special handling, and each trip has exactly one destination metro area.
- Flights, trains, buses, connections, mixed modes, different outbound/return
  sources, and different outbound/return terminals are valid. Provider-supplied
  protected connections are accepted as protected bundles. A connection that
  Crowl composes independently is unprotected and requires a configured
  minimum connection time, a trusted transfer edge for any terminal change,
  and complete transfer cost. Arbitrary within-direction cross-source leg
  chaining is not implemented yet.
- Destinations are strictly within the configured Schengen scope; the current
  strict setting also rejects non-Schengen connection terminals. Nearby
  destination terminals may count, but onward city-transfer cost/time is not
  included and the exact terminal is always shown.
- A fixed daily batch retains the cheapest valid observation for each
  city × weekend × mode (`flight`, `train`, `bus`, or `mixed`), together
  with the winning journey. Historical fares begin when batches run; they are
  not backfilled.

The UI has no accounts and no editing controls. It is intended for one owner
to configure in version-controlled JSON and share read-only with friends.

## Architecture

| Path/service | Responsibility |
|---|---|
| `services/web` | React, TypeScript, and Vite UI; Caddy serves it and proxies `/api/*` in production. |
| `services/api` | Read-only FastAPI API under `/api/v1`, health endpoints, catalog import, and development seeding. |
| `packages/crowl-core` | SQLAlchemy schema, weekend rules, trip validation, and cross-source outbound/return pairing. |
| `services/crawler` | One-shot source-isolated daily runner and transactional publisher; the fixture path remains available for development. It contains no scheduler. |
| `postgres` | PostgreSQL 17 fare history, revisions, batch coverage, journey details, and extractor health. |
| `migrations` | Alembic schema history; the API container upgrades the database before starting. |
| `data` | Version-controlled city, terminal, Schengen, and source configuration. |
| `.railway` | Railway IaC definitions for PostgreSQL, private API, public web, and a daily crawler job; they are present in the working tree but have not been committed, pushed, or applied to an owner project yet. |

Compose starts `postgres`, `api`, and `web` by default. The one-shot `crawler`
uses the `tools` profile so it does not become a long-running local service.
The Railway definition schedules the generic crawler command daily at 02:00
UTC once provisioned. With the version-controlled source kill switches it
records all 14 disabled states and publishes no invented fare; see the
deployment guide for activation requirements.

## Run locally with Compose

Prerequisites are Docker with Compose v2. The default ports are 5173 for the
web app and 8000 for the API.

```sh
cp .env.example .env
docker compose up --build -d --wait
docker compose ps
docker compose logs --tail=200 -f api web
```

Open:

- Web application: <http://localhost:5173>
- Read-only source status: <http://localhost:5173/status>
- FastAPI documentation: <http://localhost:8000/docs>
- API readiness: <http://localhost:8000/health/ready>

Stop the stack without deleting PostgreSQL data:

```sh
docker compose down
```

The equivalent shortcuts are `make up`, `make ps`, `make logs`, and
`make down`. Change `WEB_PORT` or `API_PORT` in `.env` if those ports are in
use.

### What the local data means

Non-live data has four distinct origins:

1. `data/cities.mock.json` is the temporary editorial catalog. Its city facts,
   visit flags, attributes, and photos are examples until the final ~150-city
   file is supplied.
2. The API's development seed creates a synthetic price/history grid when
   `CROWL_SEED_FIXTURES=true`. Local Compose enables this by default. Railway
   explicitly sets it to `false`.
3. The crawler's separate `fixture` adapter generates deterministic test
   offers. It is marked `is_requested=false`, is excluded from the 14-provider
   status list, and never represents a real brand.
4. Local Vite development can show clearly labeled preview fallback data if
   explicitly enabled. Docker and Railway builds disable that fallback, so an
   unavailable API produces an error instead of example fares. A successful
   but empty API response remains empty.

The UI identifies fixture/preview states as synthetic. Never treat their
prices, durations, availability, coverage, or booking details as live. Setting
`CROWL_SEED_FIXTURES=false` prevents new API seed fares; it does not remove
synthetic records already stored in the persistent PostgreSQL volume.
Operational observations always take precedence over same-day synthetic rows;
the API now exposes that provenance explicitly on map fares, history cells,
journeys, and duration samples.

Compose runs Alembic automatically. If using the API directly with an older
local SQLite `crowl.db`, `create_all` cannot upgrade that existing file: back it
up and remove it for a disposable development reset, or run Alembic against it
before starting the API.

### Publish the database-backed crawler fixture

`fixture-publish` exercises the real PostgreSQL persistence path for four
synthetic destinations across the rolling six-month horizon. It migrates and
catalog-seeds the database through the API first, disables the API's separate
fare fixture for that startup, then atomically publishes crawler results:

```sh
make fixture-publish
```

Its exact commands are:

```sh
CROWL_SEED_FIXTURES=false docker compose up --build -d --wait postgres api
CROWL_SEED_FIXTURES=false docker compose --profile tools run --rm crawler fixture-publish --pretty
```

A same-day rerun is idempotent for unchanged snapshot cells. Use
`make fixture-batch` for the smaller side-effect-free fixture and `make sources`
to inspect provider slots.

## Provider authorization state

There are 14 requested source-specific adapter slots, and **all 14 live
provider adapters are disabled; none is live or enabled**:

| State | Providers | What is needed |
|---|---|---|
| `disabled_needs_credentials` | Transavia, Vueling, FlixBus, Trenitalia France | Credentials for an approved affiliate, partner, NDC, or distribution feed. |
| `disabled_needs_authorization` | Ryanair, easyJet, Wizz Air, Volotea, BlaBlaCar Bus, Itabus, Alsa, OUIGO, SNCF Connect, Eurostar | Explicitly authorized machine-readable, retailer, GDS, or distribution access. |

The `strategy` in `data/sources.yaml` records the intended implementation
shape (`http_structured`, `http_html`, Playwright, or disabled); it does not
grant access or mean an adapter is active. Crowl does not bypass login,
CAPTCHA, bot defense, rate limiting, or other access controls. If access cannot
be obtained safely, that provider remains disabled and its missing coverage is
shown rather than fabricated.

## Replace the city catalog

The runtime currently reads `data/cities.mock.json`; keep that filename when
the final supplied JSON arrives unless the application contract is changed at
the same time.

1. Preserve the top-level `schemaVersion`, `origin`, category definitions, and
   attribute definitions, then replace the `cities` array with the final
   approximately 150-city list.
2. Give every city a stable unique ID and slug, valid IANA timezone, Schengen
   country code, coordinates, visited/wishlist flags, categories, summary, and
   a 1–5 value for every defined attribute. Definitions are data-driven; do not
   hard-code them into the UI.
3. Manually curate each photo and record meaningful alt text, creator, source
   URL, license, crop position, and `curationStatus: "curated"`. Confirm actual
   usage rights independently; schema validation cannot do that.
4. Update `data/terminals.json` with the corresponding airports, stations,
   stops, origin rules, and city-terminal links. Exact operational terminal
   mappings remain separate from editorial city data.
5. Run `make validate`. Production validation checks the JSON Schema,
   references, unique IDs/slugs, attributes, timezones, Schengen membership,
   terminal links, all 14 source IDs, and photo provenance. It does not enforce
   an approximately-150-city count, so that handoff still needs a human check.
6. Rebuild/restart the API to import the version-controlled changes:

   ```sh
   docker compose up --build -d --wait api
   ```

The contract is defined by `data/cities.schema.json`; Schengen country codes
are in `data/schengen.json`. Run validation before committing any catalog
change.

## Development and verification

Use Python 3.12 and Node.js 22. A virtual environment keeps Python dependencies
isolated:

```sh
python3.12 -m venv .venv
. .venv/bin/activate
make install
make check
```

Available checks and builds:

| Command | Runs |
|---|---|
| `make validate` | Production catalog/source validation. |
| `make test-backend` | FastAPI, domain, SQLAlchemy, and Alembic migration tests. |
| `make test-crawler` | Adapter contracts, batch behavior, health, fixture, and database publisher tests. |
| `make test-web` | Vitest UI and API-contract tests. |
| `make test-iac` | Railway TypeScript IaC type-check. |
| `make test` | All four test/check targets above. |
| `make check` | Data validation plus the full test suite. |
| `make build-web` | TypeScript check and Vite production bundle. |
| `make build` | API, web, and tools-profile crawler Docker images. |

CI additionally applies migrations to PostgreSQL, checks model/migration
drift, audits frontend dependencies, and builds each image. Production should
keep `CROWL_SEED_FIXTURES=false`, keep the API/database private behind the web
proxy, and store future provider credentials only as sealed Railway variables.

The current working tree has not been committed or pushed. Railway's GitHub
source cannot deploy it until the owner reviews the files and creates the
initial commit in the intended repository.

Alerts and a dedicated best-options page are planned extensions; they are not
part of the current release.
