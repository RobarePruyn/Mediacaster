"""Initial schema — all tables from the SQLite era

Revision ID: 001
Revises: None
Create Date: 2026-03-18
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = "001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # -- Users --
    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("username", sa.String(128), unique=True, nullable=False, index=True),
        sa.Column("hashed_password", sa.String(256), nullable=False),
        sa.Column("is_active", sa.Boolean(), default=True),
        sa.Column("is_admin", sa.Boolean(), default=False),
        sa.Column("must_change_password", sa.Boolean(), default=True),
        sa.Column("auth_provider", sa.String(64), default="local"),
        sa.Column("external_id", sa.String(256), nullable=True),
        sa.Column("created_at", sa.DateTime()),
    )

    # -- Assets --
    op.create_table(
        "assets",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("original_filename", sa.String(512), nullable=False),
        sa.Column("display_name", sa.String(512), nullable=False),
        sa.Column("file_path", sa.String(1024), nullable=False),
        sa.Column("thumbnail_path", sa.String(1024), nullable=True),
        sa.Column("asset_type", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), default="uploading"),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("transcode_progress", sa.Float(), default=0.0),
        sa.Column("duration_seconds", sa.Float(), nullable=True),
        sa.Column("source_duration_seconds", sa.Float(), nullable=True),
        sa.Column("width", sa.Integer(), nullable=True),
        sa.Column("height", sa.Integer(), nullable=True),
        sa.Column("file_size_bytes", sa.Integer(), nullable=True),
        sa.Column("owner_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("created_at", sa.DateTime()),
        sa.Column("updated_at", sa.DateTime()),
    )

    # -- Streams --
    op.create_table(
        "streams",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("name", sa.String(256), nullable=False, default="Default Stream"),
        sa.Column("multicast_address", sa.String(64), nullable=False, default="239.1.1.1"),
        sa.Column("multicast_port", sa.Integer(), nullable=False, default=5000),
        sa.Column("status", sa.String(16), default="stopped"),
        sa.Column("playback_mode", sa.String(16), default="loop"),
        sa.Column("source_type", sa.String(32), default="playlist"),
        sa.Column("ffmpeg_pid", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime()),
        sa.Column("updated_at", sa.DateTime()),
    )

    # -- Stream Items (playlist entries) --
    op.create_table(
        "stream_items",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("stream_id", sa.Integer(),
                  sa.ForeignKey("streams.id", ondelete="CASCADE"), nullable=False),
        sa.Column("asset_id", sa.Integer(),
                  sa.ForeignKey("assets.id", ondelete="CASCADE"), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False, default=0),
    )

    # -- Browser Sources --
    op.create_table(
        "browser_sources",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("stream_id", sa.Integer(),
                  sa.ForeignKey("streams.id", ondelete="CASCADE"),
                  nullable=False, unique=True),
        sa.Column("url", sa.String(2048), nullable=False, default="about:blank"),
        sa.Column("capture_audio", sa.Boolean(), default=False),
        sa.Column("display_number", sa.Integer(), nullable=True),
        sa.Column("vnc_port", sa.Integer(), nullable=True),
        sa.Column("novnc_port", sa.Integer(), nullable=True),
    )

    # -- User Stream Assignments (RBAC) --
    op.create_table(
        "user_stream_assignments",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.Integer(),
                  sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("stream_id", sa.Integer(),
                  sa.ForeignKey("streams.id", ondelete="CASCADE"), nullable=False),
    )

    # -- Server Settings --
    op.create_table(
        "server_settings",
        sa.Column("key", sa.String(128), primary_key=True),
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("server_settings")
    op.drop_table("user_stream_assignments")
    op.drop_table("browser_sources")
    op.drop_table("stream_items")
    op.drop_table("streams")
    op.drop_table("assets")
    op.drop_table("users")
