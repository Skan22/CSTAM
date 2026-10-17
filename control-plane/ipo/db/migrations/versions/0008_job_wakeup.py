"""wake idle workers when a job is queued

Revision ID: 0008
Revises: 0007
"""

from pathlib import Path

from alembic import op

revision = "0008"
down_revision = "0007"

_SQL = Path(__file__).with_suffix(".sql")


def upgrade() -> None:
    raw = op.get_bind().connection.dbapi_connection
    assert raw is not None
    raw.cursor().execute(_SQL.read_text())


def downgrade() -> None:
    raise NotImplementedError("forward-only migration")
