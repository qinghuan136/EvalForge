"""Add reusable experiment configs and retire the unused experiment job configuration tables."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "20261119_experiment_config"
down_revision = "20261118_conversation_store"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        DO $$
        DECLARE
            table_name text;
            has_rows boolean;
        BEGIN
            FOREACH table_name IN ARRAY ARRAY['experiments', 'experiment_jobs',
                                              'experiment_prompt_tasks', 'experiment_dataset_evaluators']
            LOOP
                IF to_regclass(table_name) IS NOT NULL THEN
                    EXECUTE format('SELECT EXISTS (SELECT 1 FROM %I)', table_name) INTO has_rows;
                    IF has_rows THEN
                        RAISE EXCEPTION 'Experiment config upgrade expects an empty % table', table_name;
                    END IF;
                END IF;
            END LOOP;
        END;
        $$
    """)
    op.create_table(
        "experiment_config",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("config_group_id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("description", sa.String(2000)),
        sa.Column("dataset_id", sa.Integer(), sa.ForeignKey("datasets.id", ondelete="CASCADE"), nullable=False),
        sa.Column("dataset_version_id", sa.Integer(), sa.ForeignKey("dataset_versions.id", ondelete="NO ACTION")),
        sa.Column("runner_id", sa.String(), sa.ForeignKey("runners.runner_id", ondelete="RESTRICT"), nullable=False),
        sa.Column("agent_target_id", sa.String(128), nullable=False),
        sa.Column("timeout_seconds", sa.Float(), nullable=False, server_default="60"),
        sa.Column("stream_output", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("repetitions", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("evaluators", JSONB(none_as_null=True), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("config_group_id", "version"),
        sa.CheckConstraint("version > 0", name="positive_version"),
        sa.CheckConstraint("length(trim(name)) BETWEEN 1 AND 100", name="valid_name"),
        sa.CheckConstraint("length(trim(agent_target_id)) BETWEEN 1 AND 128", name="valid_agent_target"),
        sa.CheckConstraint("timeout_seconds > 0 AND timeout_seconds <= 3600", name="valid_timeout"),
        sa.CheckConstraint("repetitions BETWEEN 1 AND 100", name="valid_repetitions"),
    )
    for column in ("user_id", "dataset_id", "dataset_version_id", "runner_id"):
        op.create_index(f"ix_experiment_config_{column}", "experiment_config", [column])
    op.add_column("experiments", sa.Column("experiment_config_id", sa.Integer(), nullable=False))
    op.create_foreign_key("fk_experiments_experiment_config_id_experiment_config", "experiments",
                          "experiment_config", ["experiment_config_id"], ["id"], ondelete="NO ACTION")
    op.create_index("ix_experiments_experiment_config_id", "experiments", ["experiment_config_id"])
    op.execute("DROP TABLE IF EXISTS experiment_prompt_tasks")
    op.execute("DROP TABLE IF EXISTS experiment_dataset_evaluators")
    op.execute("DROP TABLE IF EXISTS experiment_jobs")


def downgrade() -> None:
    raise RuntimeError("Downgrading would discard saved experiment configuration versions; restore a pre-upgrade backup.")
