"""Add per-stream encoding profiles and asset renditions table

Revision ID: 005
Revises: 004
Create Date: 2026-03-26

Adds per-stream encoding columns (resolution, codec, framerate, video_bitrate,
gop_size) to the streams table so each stream can have its own output profile.

Creates the asset_renditions table for the transcode ladder — each uploaded
asset gets multiple renditions at different resolutions/codecs, and playlist
streams use -c copy from the matching rendition.

Seeds the transcode_ladder server setting that controls which renditions are
generated on upload.
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = "005"
down_revision: Union[str, None] = "004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Per-stream encoding profile columns on the streams table.
    # All have defaults so existing streams get sensible values (1080p30 H.264).
    op.add_column("streams", sa.Column("resolution", sa.String(16), nullable=False,
                                       server_default="1920x1080"))
    op.add_column("streams", sa.Column("codec", sa.String(8), nullable=False,
                                       server_default="h264"))
    op.add_column("streams", sa.Column("framerate", sa.Integer(), nullable=False,
                                       server_default="30"))
    # Nullable: null means "use auto-default from bitrate table"
    op.add_column("streams", sa.Column("video_bitrate", sa.String(16), nullable=True))
    op.add_column("streams", sa.Column("gop_size", sa.Integer(), nullable=True))

    # Asset renditions table for the transcode ladder
    op.create_table(
        "asset_renditions",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("asset_id", sa.Integer(),
                  sa.ForeignKey("assets.id", ondelete="CASCADE"), nullable=False),
        sa.Column("resolution", sa.String(16), nullable=False),
        sa.Column("codec", sa.String(8), nullable=False),
        sa.Column("framerate", sa.Integer(), nullable=False, server_default="60"),
        sa.Column("file_path", sa.String(1024), nullable=True),
        sa.Column("file_size_bytes", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="processing"),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("transcode_progress", sa.Float(), server_default="0.0"),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
    )
    op.create_index("ix_asset_renditions_asset_id", "asset_renditions", ["asset_id"])

    # Seed the transcode ladder server setting — controls which renditions
    # are generated on upload. Default: 720p H.264 + 1080p H.264 enabled.
    op.execute(
        sa.text(
            "INSERT INTO server_settings (key, value, description) VALUES "
            "(:key, :value, :desc) ON CONFLICT (key) DO NOTHING"
        ).bindparams(
            key="transcode_ladder",
            value='{"720p_h264": true, "1080p_h264": true, "1080p_h265": false, "4k_h265": false}',
            desc="Which renditions to generate on upload (JSON object of tier: enabled pairs)",
        )
    )


def downgrade() -> None:
    op.drop_index("ix_asset_renditions_asset_id", table_name="asset_renditions")
    op.drop_table("asset_renditions")
    op.drop_column("streams", "gop_size")
    op.drop_column("streams", "video_bitrate")
    op.drop_column("streams", "framerate")
    op.drop_column("streams", "codec")
    op.drop_column("streams", "resolution")
    op.execute(sa.text("DELETE FROM server_settings WHERE key = 'transcode_ladder'"))
