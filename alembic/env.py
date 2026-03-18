"""
Alembic environment configuration.

This module connects Alembic to our SQLAlchemy models and database.
It imports the Base metadata from our models so autogenerate can detect
schema changes, and gets the database URL from our config module.
"""

import sys
from pathlib import Path
from logging.config import fileConfig
from sqlalchemy import engine_from_config, pool
from alembic import context

# Add the project root to sys.path so `from backend.xxx` imports work
# when running alembic from the command line
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.database import Base
from backend.config import DATABASE_URL
# Import all models so Base.metadata knows about them for autogenerate
import backend.models  # noqa: F401

config = context.config

# Override the sqlalchemy.url from alembic.ini with our config module's value
config.set_main_option("sqlalchemy.url", DATABASE_URL)

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode — generates SQL without connecting."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode — connects to the database and applies."""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
