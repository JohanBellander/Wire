"""session skip_reason

Revision ID: a3b4c5d6e7f8
Revises: 7124a89174a2
Create Date: 2026-05-01 09:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a3b4c5d6e7f8"  # pragma: allowlist secret
down_revision: str | Sequence[str] | None = "7124a89174a2"  # pragma: allowlist secret
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("sessions", schema=None) as batch_op:
        batch_op.add_column(sa.Column("skip_reason", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("sessions", schema=None) as batch_op:
        batch_op.drop_column("skip_reason")
