"""per-team traffic counters and recent requests

Revision ID: 0006
Revises: 0005
"""

from pathlib import Path

from alembic import op

revision = "0006"
down_revision = "0005"

_SQL = Path(__file__).with_suffix(".sql")


def upgrade() -> None:
    raw = op.get_bind().connection.dbapi_connection
    assert raw is not None
    raw.cursor().execute(_SQL.read_text())


def downgrade() -> None:
    raise NotImplementedError("forward-only migration")
