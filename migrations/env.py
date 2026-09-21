import asyncio
import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from app.models import Base  # noqa: F401  (imports all models)

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Prefer the runtime DATABASE_URL. Alembic drives the same asyncpg driver as the app,
# so the project needs no second (sync) Postgres driver installed.
_url = os.getenv("DATABASE_URL") or config.get_main_option("sqlalchemy.url") or ""
for _prefix in ("postgresql+psycopg2://", "postgresql+psycopg://", "postgresql://", "postgres://"):
    if _url.startswith(_prefix):
        _url = _url.replace(_prefix, "postgresql+asyncpg://", 1)
        break
if _url:
    config.set_main_option("sqlalchemy.url", _url)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(url=config.get_main_option("sqlalchemy.url"), target_metadata=target_metadata,
                      literal_binds=True, dialect_opts={"paramstyle": "named"})
    with context.begin_transaction():
        context.run_migrations()


def _run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


async def _run_migrations_online() -> None:
    connectable = async_engine_from_config(config.get_section(config.config_ini_section, {}),
                                           prefix="sqlalchemy.", poolclass=pool.NullPool)
    try:
        async with connectable.connect() as connection:
            await connection.run_sync(_run_migrations)
    finally:
        await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(_run_migrations_online())
