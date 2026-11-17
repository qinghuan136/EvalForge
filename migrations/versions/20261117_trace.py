"""Add OTLP trace storage and the optional attempt trace reference."""

import sqlalchemy as sa
from alembic import op

revision = "20261117_trace"
down_revision = "20261116_assistant"
branch_labels = None
depends_on = None


def _otel_id_check(column: str, digits: int, table: str) -> sa.CheckConstraint:
    return sa.CheckConstraint(
        f"length({column}) = {digits} AND {column} ~ '^[0-9a-f]{{{digits}}}$' "
        f"AND {column} <> '{'0' * digits}'",
        name=op.f(f"ck_{table}_{column}_hex"),
    )


def _time_range(table: str) -> sa.CheckConstraint:
    return sa.CheckConstraint(
        "start_time_unix_nano >= 0 AND end_time_unix_nano <= 18446744073709551615 "
        "AND end_time_unix_nano >= start_time_unix_nano",
        name=op.f(f"ck_{table}_time_range"),
    )


def upgrade() -> None:
    op.create_table(
        "traces",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("owner_user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("trace_id", sa.Text(), nullable=False),
        sa.Column("start_time_unix_nano", sa.Numeric(20, 0), nullable=False),
        sa.Column("end_time_unix_nano", sa.Numeric(20, 0), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("owner_user_id", "trace_id"),
        _otel_id_check("trace_id", 32, "traces"),
        _time_range("traces"),
    )
    op.create_index("ix_traces_owner_start_id", "traces",
                    ["owner_user_id", sa.text("start_time_unix_nano DESC"), sa.text("id DESC")])
    op.create_table(
        "spans",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("trace_row_id", sa.BigInteger(), sa.ForeignKey("traces.id", ondelete="CASCADE"), nullable=False),
        sa.Column("span_id", sa.Text(), nullable=False),
        sa.Column("parent_span_id", sa.Text()),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("kind", sa.Integer(), nullable=False),
        sa.Column("start_time_unix_nano", sa.Numeric(20, 0), nullable=False),
        sa.Column("end_time_unix_nano", sa.Numeric(20, 0), nullable=False),
        sa.Column("status_code", sa.Integer()),
        sa.Column("status_message", sa.Text()),
        sa.Column("trace_state", sa.Text(), server_default="", nullable=False),
        sa.Column("flags", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("attributes", sa.JSON(), server_default=sa.text("'[]'"), nullable=False),
        sa.Column("events", sa.JSON(), server_default=sa.text("'[]'"), nullable=False),
        sa.Column("links", sa.JSON(), server_default=sa.text("'[]'"), nullable=False),
        sa.Column("resource", sa.JSON()),
        sa.Column("instrumentation_scope", sa.JSON()),
        sa.Column("dropped_attributes_count", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("dropped_events_count", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("dropped_links_count", sa.BigInteger(), server_default="0", nullable=False),
        sa.UniqueConstraint("trace_row_id", "span_id"),
        _otel_id_check("span_id", 16, "spans"),
        _otel_id_check("parent_span_id", 16, "spans"),
        _time_range("spans"),
    )
    op.create_index("ix_spans_trace_start_id", "spans", ["trace_row_id", "start_time_unix_nano", "id"])
    op.add_column("runner_attempts", sa.Column("trace_id", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("runner_attempts", "trace_id")
    op.drop_table("spans")
    op.drop_table("traces")
