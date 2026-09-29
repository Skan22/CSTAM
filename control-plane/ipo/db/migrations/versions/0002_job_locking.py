"""job locking columns

Revision ID: 0002
Revises: 0001
"""

from pathlib import Path

from alembic import op

revision = "0002"
down_revision = "0001"

_SQL = Path(__file__).with_suffix(".sql")


def upgrade() -> None:
    raw = op.get_bind().connection.dbapi_connection
    assert raw is not None
    raw.cursor().execute(_SQL.read_text())


def downgrade() -> None:
    raise NotImplementedError("forward-only migration")
