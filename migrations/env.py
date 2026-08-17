"""Alembic environment.

The URL is read from settings, never from alembic.ini, so a connection string is
never committed and the same migration runs against dev/staging/production by
environment alone.
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.ext.asyncio import async_engine_from_config

from tutor_match_meta.config.settings import get_settings
from tutor_match_meta.repositories.models import SCHEMA, Base

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata
config.set_main_option("sqlalchemy.url", get_settings().postgres_dsn)


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        compare_type=True,
        # Our alembic_version lives in our schema. Sharing the database with
        # Demo Command Center means a single public.alembic_version would have
        # two services overwriting each other's migration head — and that
        # collision is real, not hypothetical: their alembic_version already
        # exists in public.
        version_table_schema=SCHEMA,
        include_schemas=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        # Emitted before anything else so the generated script can be run
        # top-to-bottom against an empty database.
        if SCHEMA:
            context.execute(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}")
        context.run_migrations()


def _do_run(connection) -> None:
    # The schema must exist *before* `context.configure`, because Alembic
    # creates `alembic_version` in `version_table_schema` as its very first
    # act — before running the migration that would have created the schema.
    # Against a clean database that is a chicken-and-egg failure:
    #   InvalidSchemaNameError: schema "tutor_match" does not exist
    # Idempotent, so it is a no-op on every subsequent run.
    if SCHEMA:
        connection.exec_driver_sql(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}")
        connection.commit()

    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        version_table_schema=SCHEMA,
        include_schemas=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    engine = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with engine.connect() as connection:
        await connection.run_sync(_do_run)
    await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
