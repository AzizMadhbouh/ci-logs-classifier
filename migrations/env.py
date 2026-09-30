# migrations/env.py
from logging.config import fileConfig
from sqlalchemy import engine_from_config, pool
from alembic import context
import os

config = context.config
fileConfig(config.config_file_name)

# The schema is managed by explicit op.create_table()/op.drop_table() calls
# in migrations/versions - there is no declarative model layer to reflect,
# so autogenerate is never used and no metadata is needed.
target_metadata = None


def get_url():
    return (
        f"postgresql://{os.getenv('DB_USER')}:{os.getenv('DB_PASSWORD')}@"
        f"{os.getenv('DB_HOST')}:{os.getenv('DB_PORT')}/{os.getenv('DB_NAME')}"
    )


# alembic.ini carries a %(DB_USER)s-style template that ConfigParser cannot
# resolve from the environment, so materialise the concrete URL before
# alembic reads sqlalchemy.url (escape % for ConfigParser interpolation).
config.set_main_option("sqlalchemy.url", get_url().replace("%", "%%"))


def run_migrations_offline():
    url = get_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online():
    connectable = engine_from_config(
        config.get_section(config.config_ini_section),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
