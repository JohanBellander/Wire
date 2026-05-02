"""draft original_text

Adds `drafts.original_text` to capture the first LLM-generated text when
the user starts revising via NL. Existing rows get NULL; the approve path
treats NULL as "never revised" so old drafts are unaffected.

Revision ID: b8c9d0e1f2a3
Revises: a3b4c5d6e7f8
Create Date: 2026-05-02 10:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b8c9d0e1f2a3"  # pragma: allowlist secret
down_revision: str | Sequence[str] | None = "a3b4c5d6e7f8"  # pragma: allowlist secret
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("drafts", schema=None) as batch_op:
        batch_op.add_column(sa.Column("original_text", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("drafts", schema=None) as batch_op:
        batch_op.drop_column("original_text")
