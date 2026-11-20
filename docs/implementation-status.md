# Crowl implementation status

Crowl is runnable locally today with the 12-city mock catalog and synthetic
fare history in this working tree. The mock data is intentionally retained until the
owner supplies the final city JSON. Nothing in the current UI should be
interpreted as a live provider price.

## Working now with the mock catalog

- A React/Vite read-only application with an interactive, stylized SVG overview
  map, weekend and mode filters, pan/zoom, clustering, a searchable city
  directory, city pages, a price-history heatmap, an exact journey panel, and
  an extractor-status page. The map is functional but is not yet a real
  geographic tile/boundary map.
- A FastAPI read-only API backed by PostgreSQL, with catalog import, rolling
  six-month weekends, map fares, historical cells, exact journey revisions,
  typical-duration aggregates, status reporting, readiness, and schema checks.
- Versioned city, terminal, Schengen, source, rule, and passenger-profile
  configuration. Startup reconciliation is idempotent and fails readiness if
  the catalog cannot be imported safely.
- The confirmed Friday 19:00 to Monday 07:00 window, 12-hour minimum stay,
  12-hour maximum per direction, Paris-area origins, one destination, strict
  Schengen path, provider-supplied connected itineraries, cross-source
  outbound/return pairs, and mixed-mode round trips. A provider-supplied
  protected connection is accepted as a protected bundle. Crowl-composed
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
- Explicit synthetic/operational provenance on fares, history, journeys, and
  duration aggregates. Operational observations win over cheaper mock rows for
  the same cell, while the UI labels mixed or synthetic data visibly.
- Durable daily-run bookkeeping, a full pre-network work ledger, source-level
  health, a single-run lease, interruption recovery, resumable safe empty
  outcomes, and atomic publication.
- One isolated adapter slot and status row for each of the 14 requested
  providers. **All 14 live provider adapters are disabled.** Disabled or
  unavailable sources are recorded explicitly and never produce invented
  fares.
- Docker Compose for PostgreSQL, API, web, and the one-shot crawler; synthetic
  database fixtures for local evaluation; CI checks; Alembic migrations; and
  Railway project-level infrastructure definitions for a private
  API/database, public web service, and daily crawler cron. The Railway
  definition has not been committed/pushed, provisioned, or burn-in tested.
- A manually selected and locally stored development photo for every mock city,
  with provenance fields. These are mock-catalog assets, not the final photo
  curation.

## Pending implementation work

- Replace the stylized SVG overview with a real geographic tile layer and a
  properly attributed Schengen boundary/mask, as specified in the target plan.
- Add arbitrary within-direction cross-source leg chaining. The current
  composition boundary is provider-supplied multi-leg itineraries plus pairing
  complete outbound and return components across sources and modes.
- Expand the mock fare matrix if a richer demo is desired. The database seed
  intentionally publishes one plausible terminal mode per mock city rather
  than inventing impossible routes; production mode coverage must come from
  enabled providers.

## Still requires external input or access

- The owner's final approximately 150-city JSON. Importing it will also require
  completing its exact terminal mappings and manually curating, license-checking,
  optimizing, and attributing the final photos. The current 12-city catalog and
  its locally stored photos remain mock data until then.
- Authorized machine-readable access for the requested transport providers.
  Source-specific disabled adapter slots and health states exist, but none of
  the 14 providers has an enabled live search implementation. No CAPTCHA,
  login, bot defense, or access control is bypassed.
- Real historical prices. History starts accumulating only after enabled live
  collection runs; it cannot be truthfully backfilled.
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
the API documentation is <http://localhost:8000/docs>. Local Compose enables
clearly labeled synthetic fares by default; Railway disables them.
