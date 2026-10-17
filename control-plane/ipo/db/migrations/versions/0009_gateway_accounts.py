"""gateway agent accounts

Revision ID: 0009
Revises: 0008
"""

from pathlib import Path

from alembic import op

revision = "0009"
down_revision = "0008"

_SQL = Path(__file__).with_suffix(".sql")


def upgrade() -> None:
    raw = op.get_bind().connection.dbapi_connection
    assert raw is not None
    raw.cursor().execute(_SQL.read_text())


def downgrade() -> None:
    raise NotImplementedError("forward-only migration")
