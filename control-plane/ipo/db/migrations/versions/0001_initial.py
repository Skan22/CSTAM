"""initial schema

Revision ID: 0001
"""

from pathlib import Path

from alembic import op

revision = "0001"
down_revision = None

_SQL = Path(__file__).with_suffix(".sql")


def upgrade() -> None:
    # A raw cursor uses the simple query protocol, which allows a multi-statement script.
    raw = op.get_bind().connection.dbapi_connection
    assert raw is not None
    raw.cursor().execute(_SQL.read_text())


def downgrade() -> None:
    raise NotImplementedError("forward-only migration")
