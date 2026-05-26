"""Alembic environment.

Pulls the DB URL from `app.config` (same source as the runtime engine)
so migrations always target the configured backend — SQLite by default,
MariaDB if `database.url` is set.

`render_as_batch=True` is required for SQLite's limited ALTER TABLE
support; on MariaDB it's a no-op overhead, so we only enable it when
the URL is SQLite.
"""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from app.config import get_settings
from app.models import Base
from app.paths import DB_PATH, ensure_runtime_dirs

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)


def _resolve_url() -> str:
    url = (get_settings().database.url or "").strip()
    if url:
        return url
    ensure_runtime_dirs()
    return f"sqlite:///{DB_PATH.as_posix()}"


_url = _resolve_url()
_is_sqlite = _url.startswith("sqlite")
config.set_main_option("sqlalchemy.url", _url)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=_is_sqlite,
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
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=_is_sqlite,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
