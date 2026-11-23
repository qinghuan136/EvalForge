"""Add archival timestamp for Python evaluators."""

import sqlalchemy as sa
from alembic import op

revision = "20261123_code_evaluator_archive"
down_revision = "20261123_code_evaluators"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("evaluators", sa.Column("archived_at", sa.DateTime(timezone=True)))


def downgrade() -> None:
    op.drop_column("evaluators", "archived_at")
