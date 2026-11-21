# Crowl implementation status

Crowl is runnable locally today with the supplied 150-city catalog. FlixBus is
enabled only for one verified direct Paris Bercy Seine–Amsterdam Sloterdijk
route; the other 13 providers are disabled. A fresh database starts with no
fares, but the bounded local activation on 2026-11-21 published one real
displayed-fare observation for the 2026-11-27 weekend.

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
- One isolated adapter, fail-closed public-HTML/embedded-JSON parser, sanitized
  fixture suite, and status row for each of the 14 requested providers. FlixBus
  is enabled for one bounded route; disabled, challenged, or unavailable
  sources are recorded explicitly and never produce invented fares.
- DB-free FlixBus canaries against Paris Bercy Seine–Amsterdam Sloterdijk
  completed all six date searches for both 2026-11-27 and the six-month endpoint
  2027-05-21. The rendered DOM parser accepts only exact direct-stop cards and
  applies the weekend, 12-hour direction, and 12-hour stay rules.
- The guarded activation pilot completed on 2026-11-21 and atomically published
  a €45.96 displayed fare for 2026-11-27: €18.98 outbound and €26.98 return.
  Both components are stored as `estimated`, with fees unconfirmed and no
  booking URL, and exposed as `Displayed fare — service fees may apply`. The
  run retained one snapshot/revision, two exact legs, two coverage directions,
  and all 14 source health states.
- Docker Compose for PostgreSQL, API, web, and the one-shot crawler; CI checks;
  Alembic migrations; and Railway project-level infrastructure definitions for
  a private API/database, public web service, and daily crawler cron. The Railway
  definition has not been committed/pushed, provisioned, or burn-in tested.
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
- Complete operational terminal mappings for the 138 catalog cities that do
  not yet have one. Those cities remain fully available to the map, directory,
  and city pages, but cannot receive crawler coverage until linked.

## Still requires live-page verification or external input

- Per-source verification of stable ordinary public result markup, exact-fare
  semantics, terminal aliases, explicit route coverage, conservative limits,
  and booking domains for the remaining 13 disabled providers and any new
  FlixBus route. The extractors use public HTML or JSON embedded in it,
  directly or after ordinary Playwright rendering; they do not call provider,
  partner, affiliate, GDS, or undocumented browser APIs. Normal cookie-consent
  handling is allowed. CAPTCHA, login, persistent rate limiting, anti-bot
  defenses, and access controls are not bypassed; difficult sources stay
  skipped and kill-switched.
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
