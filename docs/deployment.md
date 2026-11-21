# Deploy Crowl on Railway

This repository uses Railway's current project-level Infrastructure as Code
(IaC) workflow. The desired project is defined in
`.railway/railway.ts`: PostgreSQL, the private API, the public web service, and
the daily crawler job.

Do not add `railway.json` or `railway.toml`. Railway marks that older
single-service Config as Code system as deprecated; new services cannot opt in,
and legacy files stop being read on 1 December 2026. See the official
[IaC guide](https://docs.railway.com/infrastructure-as-code) and
[TypeScript reference](https://docs.railway.com/infrastructure-as-code/reference).

## What the definition creates

| Resource | Source/build context | Exposure | Runtime behavior |
|---|---|---|---|
| `postgres` | Railway PostgreSQL helper | Private only | Supplies its private `DATABASE_URL` by a typed reference. |
| `api` | Repository root; `services/api/Dockerfile` | Private only | Listens on port 8000 and is deployment-gated by `/health/ready`. |
| `web` | `services/web` | Public after a domain is generated | Caddy serves React and proxies `/api/*` to `api.railway.internal:8000`. |
| `crawler` | Repository root; `services/crawler/Dockerfile` | No domain | Runs the configured six-month provider manifest once per day, records disabled/failing source states, publishes validated winners atomically, and exits. |

The different build roots are intentional. Railway supports a root directory
per monorepo service, and watch paths remain relative to the repository root.
The API and crawler use the repository root because their Dockerfiles copy the
shared `packages/crowl-core`; the API also copies `data`. See Railway's
[monorepo guidance](https://docs.railway.com/deployments/monorepo),
[build configuration](https://docs.railway.com/builds/build-configuration), and
[Dockerfile documentation](https://docs.railway.com/builds/dockerfiles).

Only `web` needs public networking. It reaches the API through Railway's
environment-isolated private network, whose service DNS convention is
`SERVICE_NAME.railway.internal`; the database is also referenced privately.
See [Private Networking](https://docs.railway.com/networking/private-networking).

## Prerequisites

- Node.js 22 or newer.
- The current Railway CLI, authenticated to the intended workspace.
- A GitHub repository Railway can access.
- A new empty Railway project, or an existing project that has first been
  imported and reconciled as described below.

Install and validate the IaC dependency:

```sh
npm ci --prefix .railway
npm run --prefix .railway check
railway upgrade
railway login
railway whoami
```

The workspace lockfile pins the generally available TypeScript SDK. No
application dependency is installed by this step.

## Authoring inputs

The repository is deliberately not hard-coded. Set these variables in the
shell that runs `railway config plan` or `railway config apply`:

| Variable | Required | Meaning |
|---|---:|---|
| `CROWL_RAILWAY_GITHUB_REPO` | Yes locally | GitHub `owner/repository`. In GitHub Actions, `GITHUB_REPOSITORY` is accepted instead. |
| `CROWL_RAILWAY_GITHUB_BRANCH` | No | Deployment branch; defaults to `main`. |
| `CROWL_RAILWAY_PROJECT_NAME` | No | Desired project name; otherwise the linked project name, then `crowl`. |

For example:

```sh
export CROWL_RAILWAY_GITHUB_REPO=your-owner/your-repository
export CROWL_RAILWAY_GITHUB_BRANCH=main
```

These are local IaC authoring inputs, not application secrets and not Railway
service variables. The definition fails before planning if the repository is
missing or is not in `owner/repository` form, preventing a placeholder source
from being applied.

## First deployment

From the repository root:

```sh
railway init
# Or, for an existing project:
# railway link

railway config plan
railway config apply
railway config plan
```

Read the first plan carefully, especially removals. `plan` is read-only;
`apply` plans again and asks for confirmation. Railway also rejects stale
plans. The final plan should report no drift. Those safeguards and the
non-interactive saved-plan workflow are documented in
[Railway Infrastructure as Code](https://docs.railway.com/infrastructure-as-code).

For an existing Railway project, first run `railway config pull` and reconcile
the imported desired state rather than applying this file blindly. A complete
project definition owns the selected environment, so omitting an existing
resource can propose its removal.

Railway-provided domains are not represented in the IaC definition. Generate
one for the web service after its first successful deployment:

```sh
railway domain --service web --port 80
railway domain list --service web
```

The [domain command](https://docs.railway.com/cli/domain) creates the public
`*.up.railway.app` endpoint. Do not create public domains or TCP proxies for
`api`, `crawler`, or `postgres`.

## Runtime variables

All variables needed by the current scaffold are declared in the IaC graph:

| Service | Variables |
|---|---|
| `api` | Private PostgreSQL `DATABASE_URL`, `PORT=8000`, catalog path, migration/seed flags, freshness threshold, 180-day duration-history horizon, and expected schema revision. Synthetic fixture fares are explicitly disabled. |
| `web` | `PORT=80` and private `API_URL=http://api.railway.internal:8000`. |
| `crawler` | Private PostgreSQL `DATABASE_URL`, 20-hour deadline, strict Schengen-path flag, log level, and the bounded public FlixBus search template, aliases, and route coverage. |

`CROWL_DURATION_HISTORY_DAYS=180` bounds the daily duration histograms used for
city-page p25/median/p75 estimates. Only validated observations inside that
rolling window are counted.

Provider credentials are intentionally unsupported. FlixBus is enabled only
for the verified direct Bercy Seine–Sloterdijk route; the other 13 adapters are
disabled until their ordinary public result pages, terminal aliases, and route
coverage have been verified. Add only documented public-page and coverage
settings as Railway service variables; never store login credentials, cookies,
or persistent browser state. Railway documents typed cross-service references
and sealed variables in [Using Variables](https://docs.railway.com/variables).
The committed FlixBus sale coverage is verified through the return date
2027-05-24; refresh the canary and coverage value before that boundary becomes
the rolling horizon's limiting date.

Railway runs `alembic upgrade head` as the API service's pre-deploy command,
then starts the image with Uvicorn only. Alembic takes a PostgreSQL advisory
lock for the whole migration connection, so concurrent deployment attempts
serialize safely. Local Compose supplies an equivalent migrate-then-start
command override. The API's idempotent startup seed imports catalog cities,
terminals, rules, sources, and the rolling weekend window. It does not call
SQLAlchemy `create_all`, and
`CROWL_SEED_FIXTURES=false` prevents development prices from entering Railway.
The API remains a single replica for the initial deployment.

## Daily crawler schedule

The crawler is configured with:

```text
cronSchedule: 0 2 * * *
restartPolicyType: NEVER
```

Railway cron expressions use UTC, so this is one run every day at **02:00
UTC**, regardless of daylight-saving changes in Paris. Railway may start a cron
a few minutes after the nominal time. A cron service must close resources and
exit; if the preceding execution is still active at the next scheduled time,
Railway skips the new run. See the official
[Cron Jobs documentation](https://docs.railway.com/cron-jobs).

The scheduled command is `crowl-crawler run`. It resolves the real catalog and
six-month manifest, searches only the enabled FlixBus coverage, records the
other 13 providers as disabled with version-controlled reasons, and never
substitutes fixture fares. Enabling another source or route requires verified
HTML markup and coverage plus an explicit source-config change; the publication
contract does not change. `fixture-publish` remains a manual local integration
command and is never the Railway schedule.

Inspect or trigger the job without changing its schedule:

```sh
railway cron list --service crawler
railway cron run --service crawler
```

## Verification

After the services deploy:

1. Open the generated web domain and verify `/healthz` returns `200`.
2. Load `/` and confirm browser requests to `/api/v1/*` succeed through Caddy.
3. Check the API deployment healthcheck and confirm `/health/ready` is healthy.
4. Run the crawler once with `railway cron run --service crawler`, then inspect
   it with `railway cron list --service crawler` and the deployment logs.
5. Run `railway config plan` again and confirm there is no drift.

Railway healthchecks gate a new deployment but are not continuous monitoring.
Use the Crowl status data and an external uptime monitor for ongoing detection;
see [Railway Healthchecks](https://docs.railway.com/deployments/healthchecks).

## Backups, restore, and first-week acceptance

Fare history is permanent product data, so a successful deployment is not the
end of the setup. In the Railway dashboard, enable scheduled backups for the
PostgreSQL volume before the first scheduled live run. Keep a retention period
that covers accidental catalog or migration damage, and record the selected
policy outside the database itself. Railway's current backup controls and
restore flow are documented under [Volume Backups](https://docs.railway.com/volumes/backups).

Test recovery rather than assuming a backup is usable:

1. Create an on-demand backup after the catalog import and crawler smoke test.
2. Restore it into a separate temporary environment, never over production.
3. Run `alembic current`, `/health/ready`, and the catalog/source counts there.
4. Compare fare snapshot, revision, booking, leg, observation, and crawl-run
   counts with the source environment.
5. Delete the temporary restore only after the comparison is recorded.

Before calling production stable, observe seven consecutive scheduled crawler
runs. Each day verify exactly one crawl run, a fresh heartbeat/completion, no
duplicate cell observations, explicit disabled/failure states, and no
unlabelled loss of a previously trusted fare. Actual backup activation, domain
creation, provider configuration, and this seven-run burn-in require access to
the owner's Railway project and therefore cannot be performed from the local
repository.
