"""Flatten Python evaluator storage and number published source versions."""

import sqlalchemy as sa
from alembic import op

revision = "20261123_code_evaluators"
down_revision = "20261119_experiment_config"
branch_labels = None
depends_on = None

VERSIONS = "code_evaluator_code_versions"


def upgrade() -> None:
    op.add_column("evaluators", sa.Column("draft_source_code", sa.Text()))
    op.create_check_constraint(op.f("ck_evaluators_code_evaluator_has_owner"),
                               "evaluators", "kind <> 'CODE' OR user_id IS NOT NULL")

    op.add_column(VERSIONS, sa.Column("version_number", sa.Integer()))
    op.execute("""
        WITH numbered AS (
            SELECT id, row_number() OVER (PARTITION BY code_evaluator_id ORDER BY id) AS version_number
            FROM code_evaluator_code_versions
        )
        UPDATE code_evaluator_code_versions AS versions
        SET version_number = numbered.version_number
        FROM numbered WHERE versions.id = numbered.id
    """)
    op.alter_column(VERSIONS, "version_number", existing_type=sa.Integer(), nullable=False)
    op.alter_column(VERSIONS, "source_code", existing_type=sa.String(), type_=sa.Text(), server_default=None)
    op.drop_index("ix_code_evaluator_code_versions_code_evaluator_id_id", table_name=VERSIONS)
    op.drop_constraint(op.f("fk_code_evaluator_code_versions_code_evaluator_id_code_evaluators"),
                       VERSIONS, type_="foreignkey")
    op.alter_column(VERSIONS, "code_evaluator_id", new_column_name="evaluator_id")
    op.drop_column(VERSIONS, "user_id")
    op.create_foreign_key(None, VERSIONS, "evaluators", ["evaluator_id"], ["id"], ondelete="CASCADE")
    op.create_unique_constraint(None, VERSIONS, ["evaluator_id", "version_number"])
    op.create_check_constraint(op.f("ck_code_evaluator_code_versions_positive_version_number"),
                               VERSIONS, "version_number > 0")
    op.drop_table("code_evaluators")


def downgrade() -> None:
    raise RuntimeError("Restore a pre-upgrade backup to recover the old evaluator subtype configuration.")
