import os
from logging.config import fileConfig
from sqlalchemy import engine_from_config, pool, create_engine
from alembic import context

from app.models import Base
import app.models as _models  # noqa: F401  # force model module import side effects

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata

# Grab the URL from the Vercel environment variables
# (If your Neon URL uses a different variable name like POSTGRES_URL, change it here)
DATABASE_URL = os.getenv("DATABASE_URL")

def run_migrations_offline() -> None:
    context.configure(
        url=DATABASE_URL,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()

def run_migrations_online() -> None:
    if not DATABASE_URL:
        raise ValueError("DATABASE_URL environment variable is not set. Are you using your Vercel wrapper?")
    
    # We create the engine directly using the Env Var
    connectable = create_engine(DATABASE_URL, poolclass=pool.NullPool)

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            compare_server_default=True,
        )

        with context.begin_transaction():
            context.run_migrations()

if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()