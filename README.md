# Crowl

Crowl (crow + crawl) is a private, read-only weekend travel monitor for the
Schengen area. It compares complete round-trip prices from Paris-area
terminals, retains daily price history, and presents destinations on an
interactive map and city pages.

This repository contains a runnable local v1 backed by the supplied,
manually curated 150-city catalog. Paced public-HTML inventories classify every
city for both active sources: FlixBus enables 58 Paris routes whose advertised
minimum is within Crowl's 12-hour limit, while Ryanair enables 41 city mappings
across 29 Paris-Beauvais airport routes. The other 12 requested providers
remain kill-switched. Bounded activations have published real FlixBus and
Ryanair observations for the 2026-11-27 weekend.

The current overview uses MapLibre with an attributed OpenFreeMap geographic
basemap, city markers, pan/zoom, clustering, keyboard controls, and an
equivalent city list. A dedicated Schengen boundary/mask is still pending.
Current cross-source composition can pair outbound and return components from
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
  The explicit FlixBus exception stores the exact public result-card amount as
  a displayed fare even if a later service fee may apply; the UI labels it and
  an exact-trip booking link is optional.
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
| `services/api` | Read-only FastAPI API under `/api/v1`, health endpoints, and catalog import. |
| `packages/crowl-core` | SQLAlchemy schema, weekend rules, trip validation, and cross-source outbound/return pairing. |
| `services/crawler` | One-shot source-isolated daily runner and transactional publisher; the fixture path remains available for development. It contains no scheduler. |
| `postgres` | PostgreSQL 17 fare history, revisions, batch coverage, journey details, and extractor health. |
| `migrations` | Alembic schema history; the API container upgrades the database before starting. |
| `data` | Version-controlled city, terminal, Schengen, and source configuration. |
| `.railway` | Railway IaC definitions for PostgreSQL, private API, public web, and a daily crawler job. |

Compose starts `postgres`, `api`, and `web` by default. The one-shot `crawler`
uses the `tools` profile so it does not become a long-running local service.
The Railway definition schedules the generic crawler command daily at 02:00
UTC once provisioned. It will run the version-controlled FlixBus and Ryanair
route catalogs and record the other 12 sources as disabled; see the deployment
guide for the remaining activation requirements.

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

`data/crowl-cities-150.json` is the canonical editorial catalog. Local startup
imports its 150 cities into PostgreSQL, while `data/terminals.json` separately
defines operational terminals and the city links curated so far. A city
without a terminal link remains visible in the directory and on the map, but
cannot receive crawler coverage until that mapping is added.

Local Compose does not seed example fares, and the frontend has no fallback
catalog or fare data. A fresh database therefore has empty fare and history
views until collection publishes an observation. The crawler's deterministic
fixture commands remain available only as explicit development/test tools;
they are never part of the normal Compose or Railway startup path.

Compose runs Alembic automatically. If using the API directly with an older
local SQLite `crowl.db`, `create_all` cannot upgrade that existing file: back it
up and remove it for a disposable development reset, or run Alembic against it
before starting the API.

## Provider extractor state

There are 14 source-specific public-page parsers. Their sanitized fixtures and
failure handling are tested. **FlixBus and Ryanair are the enabled live
extractors.** Both use ordinary Playwright rendering. FlixBus parses exact
direct-stop cards; Ryanair parses every rendered flight card and publishes the
cheapest eligible Basic-fare round trip.
Crowl can also use documented official fare-search interfaces where the
provider permits the planned usage.

Before another source or route can be enabled, verify its live results, exact
fare semantics, terminal aliases, explicit route coverage, access terms,
rate limits, and booking-link hosts. Sources without verified access or
pricing remain visible on `/status`.

The `strategy` in `data/sources.yaml` records the intended implementation
shape (`http_structured`, `http_html`, Playwright, or disabled); it does not mean an adapter is
active. A normal cookie-consent control may be accepted. Crowl does not bypass
login, CAPTCHA, bot defense, rate limiting, or other access controls. A source
that presents one of those barriers is skipped and remains disabled; missing
coverage is shown rather than fabricated.

## Maintain the city catalog

The runtime reads `data/crowl-cities-150.json` as the canonical catalog.

1. Preserve the top-level `schemaVersion`, `origin`, category definitions, and
   attribute definitions when updating the city list.
2. Give every city a stable unique ID and slug, valid IANA timezone, Schengen
   country code, coordinates, visited/wishlist flags, categories, summary, and
   a 1–5 value for every defined attribute. Definitions are data-driven; do not
   hard-code them into the UI.
3. Manually curate each photo and record meaningful alt text, creator, source
   URL, license, crop position, and `curationStatus: "curated"`. Curated HTTPS
   assets on Wikimedia's image hosts are accepted without local AVIF/WebP
   derivatives. Confirm actual usage rights independently; schema validation
   cannot do that.
4. Add airports, stations, stops, origin rules, and city-terminal links to
   `data/terminals.json` as crawler coverage is implemented. Exact operational
   terminal mappings remain separate from editorial city data; missing links
   do not invalidate otherwise usable editorial cities.
5. Run `make validate`. Production validation checks the JSON Schema,
   references, unique IDs/slugs, attributes, timezones, Schengen membership,
   every supplied terminal link, all 14 source IDs, and photo provenance.
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
keep `CROWL_SEED_FIXTURES=false` and keep the API/database private behind the
web proxy. Public-page extractor configuration belongs in sealed Railway
variables even though it must not contain login credentials or session state.

Railway deploys committed changes from the configured GitHub branch. See the
deployment guide for project provisioning and scheduled-run verification.

Alerts and a dedicated best-options page are planned extensions; they are not
part of the current release.
