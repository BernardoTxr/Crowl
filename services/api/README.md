# Crowl API

The API is a read-only FastAPI service. From the repository root, install its
dependencies and expose the shared package on `PYTHONPATH`:

```sh
python -m pip install -r services/api/requirements-dev.txt
PYTHONPATH=packages/crowl-core:services/api uvicorn app.main:app --reload
```

SQLite is the zero-configuration development fallback (`sqlite:///./crowl.db`).
It creates the schema, imports `CROWL_DATA_DIR`, and seeds an explicitly
synthetic fare grid by default. Control these independently with:

- `CROWL_AUTO_CREATE_SCHEMA`
- `CROWL_AUTO_SEED`
- `CROWL_SEED_FIXTURES`

`create_all` creates new SQLite databases but does not upgrade an existing
file. After a schema change, back up and remove the development-only
`crowl.db`, then restart, or use an Alembic-managed SQLite database from the
beginning. Readiness checks required columns and returns 503 for a stale file
instead of serving it as current.

For PostgreSQL all three default to false. Run Alembic with migration-owner
credentials, then seed only the version-controlled catalog when desired:

```sh
MIGRATION_DATABASE_URL=postgresql+psycopg://... \
  alembic -c migrations/alembic.ini upgrade head
CROWL_AUTO_SEED=true CROWL_SEED_FIXTURES=false python -m app.seed
```

Railway's `postgres://` and `postgresql://` URLs are normalized to SQLAlchemy's
`postgresql+psycopg://` driver automatically. The public API has GET endpoints
only; production should give it a database role with `SELECT` access only.
