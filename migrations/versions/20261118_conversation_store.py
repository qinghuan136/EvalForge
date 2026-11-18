"""Add ConversationStore records and shared message entries without removing legacy history."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20261118_conversation_store"
down_revision = "20261117_trace"
branch_labels = None
depends_on = None


def _json():
    return sa.JSON(none_as_null=True).with_variant(postgresql.JSONB(none_as_null=True), "postgresql")


def _check(table, expression, name):
    return sa.CheckConstraint(expression, name=op.f(f"ck_{table}_{name}"))


def upgrade() -> None:
    entries = "assistant_conversation_entries"
    records = "assistant_conversation_records"
    op.create_table(
        entries,
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("parent_id", sa.Text(), sa.ForeignKey(f"{entries}.id", ondelete="RESTRICT")),
        sa.Column("message", _json(), nullable=False),
        sa.Column("kind", sa.Text(), server_default="message", nullable=False),
        _check(entries, "length(trim(id)) > 0", "id_nonblank"),
        _check(entries, "parent_id IS NULL OR parent_id <> id", "not_own_parent"),
        _check(entries, "kind = 'message'", "kind"),
    )
    op.create_index("ix_assistant_conversation_entries_parent_id", entries, ["parent_id"])
    op.create_table(
        records,
        sa.Column("conversation_id", sa.Text(), primary_key=True),
        sa.Column("head_id", sa.Text(), sa.ForeignKey(f"{entries}.id", ondelete="RESTRICT")),
        sa.Column("revision", sa.Text(), nullable=False),
        sa.Column("scope", sa.Text(), nullable=False),
        sa.Column("usage", _json(), server_default=sa.text("'{}'"), nullable=False),
        sa.Column("deferred_tool_requests", _json()),
        sa.Column("forked_from", sa.Text()),
        sa.Column("forked_at", sa.Text(), sa.ForeignKey(f"{entries}.id", ondelete="RESTRICT")),
        sa.Column("branches", _json(), server_default=sa.text("'[]'"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("format_version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("title", sa.String(200), server_default="新对话", nullable=False),
        sa.Column("status", sa.String(24), server_default="completed", nullable=False),
        sa.Column("active_run_id", sa.Uuid()),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True)),
        sa.Column("resume_model_name", sa.String(200)),
        sa.Column("last_error", _json()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        _check(records, "length(trim(conversation_id)) > 0", "id_nonblank"),
        _check(records, "length(trim(revision)) > 0", "revision_nonblank"),
        _check(records, "scope = 'user:' || CAST(user_id AS TEXT)", "owner_scope"),
        _check(records, "length(trim(title)) > 0", "title_nonblank"),
        _check(records, "format_version = 1", "format_version"),
        _check(records, "status IN ('completed', 'running', 'awaiting_approval', 'failed')", "status"),
        _check(records, "(active_run_id IS NULL) = (lease_expires_at IS NULL)", "lease_pair"),
        _check(records, "(status = 'running') = (active_run_id IS NOT NULL)", "running_lease"),
        _check(records, "status <> 'awaiting_approval' OR deferred_tool_requests IS NOT NULL", "approval_requests"),
        _check(records, "resume_model_name IS NULL OR length(trim(resume_model_name)) > 0", "model_nonblank"),
    )
    for column in ("head_id", "forked_at"):
        op.create_index(f"ix_{records}_{column}", records, [column])
    for column, label in (("scope", "scope"), ("user_id", "user")):
        op.create_index(f"ix_{records}_{label}_updated_id", records,
                        [column, sa.text("updated_at DESC"), sa.text("conversation_id DESC")])


def downgrade() -> None:
    op.drop_table("assistant_conversation_records")
    op.drop_table("assistant_conversation_entries")
