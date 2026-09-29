from alembic import context
from sqlalchemy import create_engine
from sqlalchemy.pool import NullPool

url = context.config.get_main_option("sqlalchemy.url")
assert url, "sqlalchemy.url must be set"

# NullPool: no connection may outlive the migration (a template database must be idle).
engine = create_engine(url, poolclass=NullPool)
with engine.connect() as connection:
    context.configure(connection=connection, target_metadata=None)
    with context.begin_transaction():
        context.run_migrations()
engine.dispose()
