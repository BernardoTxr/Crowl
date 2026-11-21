# Crowl implementation status

Crowl is runnable locally with the supplied 150-city catalog. FlixBus is
enabled for direct trips from Paris Bercy Seine to Amsterdam Sloterdijk and
Brussels Gare du Midi. The other 13 providers are disabled. A fresh database
starts collecting history with its first successful scheduled run.

## Working now

- A React/Vite read-only application with a MapLibre/OpenFreeMap geographic
  overview, weekend and mode filters, pan/zoom, clustering, a searchable city
  directory, city pages, a price-history heatmap, an exact journey panel, and
  an extractor-status page. The map does not yet include a dedicated Schengen
  boundary or non-Schengen mask.
- A FastAPI read-only API backed by PostgreSQL, with catalog import, rolling
  six-month weekends, map fares, historical cells, exact journey revisions,
  typical-duration aggregates, status reporting, readiness, and schema checks.
- Versioned city, terminal, Schengen, source, rule, and passenger-profile
  configuration. Startup reconciliation is idempotent and fails readiness if
  the catalog cannot be imported safely.
- The confirmed Friday 19:00 to Monday 07:00 window, 12-hour minimum stay,
  12-hour maximum per direction, Paris-area origins, one destination, strict
  Schengen path, explicitly segmented provider connections, cross-source
  outbound/return pairs, and mixed-mode round trips. Airline connections are
  accepted only when every leg is present in embedded page data; generic or
  collapsed connected rail/bus markup is rejected. Crowl-composed
  unprotected connections require configured connection time, trusted terminal
  transfer rules, and complete transfer cost.
- Permanent daily best-fare revisions by city, weekend, and mode, while keeping
  current crawl availability separate from an older trustworthy fare.
- Auditable ticket components, terminals, legs, taxes/fee assertions,
  passenger eligibility, original currency, persisted FX evidence, safe
  booking-link policy, observation time, coverage, freshness, and diagnostics.
- A daily, fail-closed ECB reference-rate sync for non-EUR offers. A missing,
  stale, malformed, or unavailable rate rejects the affected offer and records
  a diagnostic instead of publishing a guessed EUR price.
- Durable daily-run bookkeeping, a full pre-network work ledger, source-level
  health, a single-run lease, interruption recovery, resumable safe empty
  outcomes, and atomic publication.
- Fourteen provider adapters and status rows. Invalid or missing coverage
  fails its source while verified sources continue. The coordinator applies
  per-source concurrency and allows each active search to use its own timeout
  within the overall batch deadline.
- DB-free FlixBus canaries verified both Amsterdam and Brussels with six dated
  pages per round trip. The Brussels canaries returned displayed fares of
  €27.46 for 2026-11-27 and €25.96 for 2027-05-21. The rendered DOM parser
  checks exact direct stops, weekend timing, and the 12-hour limits.
- A live daily-run check against a disposable PostgreSQL database completed
  all six FlixBus tasks for the 2026-11-27 weekend and published two snapshots:
  Amsterdam at €45.96 and Brussels at €27.46. This check used the installed
  Chrome browser and a sale horizon limited to that weekend.
- The guarded activation pilot completed on 2026-11-21 and atomically published
  a €45.96 displayed fare for 2026-11-27: €18.98 outbound and €26.98 return.
  Both components are stored as `estimated`, with fees unconfirmed and no
  booking URL, and exposed as `Displayed fare — service fees may apply`. The
  run retained one snapshot/revision, two exact legs, two coverage directions,
  and all 14 source health states.
- Docker Compose for PostgreSQL, API, web, and the one-shot crawler; CI checks;
  Alembic migrations; and Railway project-level infrastructure definitions for
  a private API/database, public web service, and daily crawler cron. Railway
  provisioning and scheduled-run verification are pending.
- The supplied 150-city editorial catalog, with categories, attributes,
  visited/wishlist state, coordinates, and manually curated Wikimedia photo
  metadata. Remote image assets are permitted without generated local
  derivatives.

## Pending implementation work

- Add a dedicated Schengen boundary/non-Schengen mask to the already attributed
  MapLibre geographic tile layer, as specified in the target plan.
- Add arbitrary within-direction cross-source leg chaining. The current
  composition boundary is provider-supplied multi-leg itineraries plus pairing
  complete outbound and return components across sources and modes.
- Complete operational terminal mappings for the 136 catalog cities that do
  not yet have one. Those cities remain fully available to the map, directory,
  and city pages, but cannot receive crawler coverage until linked.

## Still requires live-page verification or external input

- The Ryanair rendered public fare canary and OUIGO official MCP returned
  exact live results. Enabling their booking links on Crowl requires provider
  authorization. The Ryanair MCP documentation describes fare tools, but the
  live tool list only exposed network and route tools when tested.
  [OUIGO sales-site terms](https://www.ouigo.com/infos-legales-site-de-ventes) ·
  [Ryanair terms](https://www.ryanair.com/gb/en/lp/legal/terms-of-use) ·
  [Ryanair MCP docs](https://services-api.ryanair.com/mcp-server/docs)
- Per-source verification of exact fares, terminal aliases, explicit route
  coverage, usage terms, limits, and booking domains. A documented official
  fare-search interface is an option when the provider permits the planned
  usage. Public-page adapters use ordinary HTTP or Playwright rendering.
  A CAPTCHA, login challenge, or rate limit is reported as a source failure
  or access restriction.
- Production history. The one local activation observation proves the path;
  history in a fresh Railway database starts only after its own scheduled live
  runs and cannot be truthfully backfilled.
- Reviewing, committing, and pushing the current working tree, then applying
  the Railway definition to the owner's Railway/GitHub
  accounts, generating the public domain, enabling database backups, testing a
  restore, and observing the first seven scheduled runs. No Railway resources
  have been provisioned by this repository work.
- Final browser/device visual acceptance. Automated component, API-contract,
  build, and HTTP smoke tests exist, but device-level acceptance still needs a
  browser available to the release environment.

## Intentionally deferred

- Email alerts and their delivery/opt-out workflow.
- A separate best-options page and prediction/recommendation model.
- Accounts, UI editing, additional origins, door-to-door transfers, paid
  baggage, memberships, and public-holiday rules.

## Local evaluation

Run `docker compose up --build -d --wait`, then open
<http://localhost:5173>. The status page is <http://localhost:5173/status> and
the API documentation is <http://localhost:8000/docs>. A fresh Compose or
Railway database starts without fabricated fares; an existing local volume may
contain the real activation observation described above.
