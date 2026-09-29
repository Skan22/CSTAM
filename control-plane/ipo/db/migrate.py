"""Apply Alembic migrations programmatically."""

from pathlib import Path

from alembic import command
from alembic.config import Config


def to_sqlalchemy_url(dsn: str) -> str:
    return dsn.replace("postgresql://", "postgresql+psycopg://", 1)


def upgrade(dsn: str, revision: str = "head") -> None:
    cfg = Config()
    cfg.set_main_option("script_location", str(Path(__file__).parent / "migrations"))
    cfg.set_main_option("sqlalchemy.url", to_sqlalchemy_url(dsn).replace("%", "%%"))
    command.upgrade(cfg, revision)
