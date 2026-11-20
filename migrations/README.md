# Crowl database migrations

Run migrations from the repository root with the migration-owner database URL:

```sh
MIGRATION_DATABASE_URL=postgresql+psycopg://... \
  alembic -c migrations/alembic.ini upgrade head
```

The API's runtime role should have `SELECT` access only. Development SQLite may
use `CROWL_AUTO_CREATE_SCHEMA=true`; production readiness requires the Alembic
revision declared by `CROWL_SCHEMA_REVISION`.
