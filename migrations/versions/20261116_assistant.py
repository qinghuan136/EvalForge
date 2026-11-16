"""Add assistant sessions and messages to the existing application schema.

Revision ID: 20261116_assistant
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "20261116_assistant"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "assistant_sessions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("title", sa.String(200), server_default="新对话", nullable=False),
        sa.Column("version", sa.Integer(), server_default="0", nullable=False),
        sa.Column("active_run_id", sa.Uuid()),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("version >= 0", name="version"),
        sa.CheckConstraint("(active_run_id IS NULL) = (lease_expires_at IS NULL)", name="lease_pair"),
    )
    op.create_index("ix_assistant_sessions_user_updated_id", "assistant_sessions",
                    ["user_id", sa.text("updated_at DESC"), sa.text("id DESC")])
    op.create_table(
        "assistant_messages",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("session_id", sa.Integer(), sa.ForeignKey("assistant_sessions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("content", JSONB(), nullable=False),
        sa.Column("status", sa.String(16), server_default="completed", nullable=False),
        sa.Column("model_provider", sa.String(100)),
        sa.Column("model_name", sa.String(200)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("role IN ('user', 'assistant', 'tool')", name="role"),
        sa.CheckConstraint("status IN ('completed', 'interrupted', 'failed')", name="status"),
        sa.CheckConstraint(
            "(role = 'assistant' AND model_provider IS NOT NULL AND model_name IS NOT NULL) OR "
            "(role <> 'assistant' AND model_provider IS NULL AND model_name IS NULL)",
            name="role_model",
        ),
    )
    op.create_index("ix_assistant_messages_session_id_id", "assistant_messages", ["session_id", "id"])


def downgrade() -> None:
    op.drop_table("assistant_messages")
    op.drop_table("assistant_sessions")
