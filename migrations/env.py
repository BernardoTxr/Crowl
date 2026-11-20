from __future__ import annotations

import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool, text

from crowl_core.db import Base, normalize_database_url
from crowl_core import models  # noqa: F401 - registers model metadata


config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

database_url = os.getenv("MIGRATION_DATABASE_URL") or os.getenv("DATABASE_URL")
if database_url:
    config.set_main_option("sqlalchemy.url", normalize_database_url(database_url).replace("%", "%%"))

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        render_as_batch=config.get_main_option("sqlalchemy.url").startswith("sqlite"),
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        is_postgresql = connection.dialect.name == "postgresql"
        if is_postgresql:
            # Session-level locking serializes every Crowl migration entry
            # point (CI, Compose, and Railway pre-deploy) without relying on
            # a particular platform's replica behavior.
            connection.execute(
                text("SELECT pg_advisory_lock(hashtext('crowl_alembic_migration'))")
            )
            # The SELECT starts SQLAlchemy's implicit transaction. Commit it
            # before handing the connection to Alembic; session advisory locks
            # remain held across commits, while Alembic can now own and commit
            # its migration transaction.
            connection.commit()
        try:
            context.configure(
                connection=connection,
                target_metadata=target_metadata,
                compare_type=True,
                render_as_batch=connection.dialect.name == "sqlite",
            )
            with context.begin_transaction():
                context.run_migrations()
        finally:
            if is_postgresql:
                connection.execute(
                    text("SELECT pg_advisory_unlock(hashtext('crowl_alembic_migration'))")
                )
                connection.commit()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
