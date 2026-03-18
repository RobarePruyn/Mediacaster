"""Add folders table and folder_id to assets

Revision ID: 002
Revises: 001
Create Date: 2026-03-18
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = "002"
down_revision: Union[str, None] = "001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # -- Folders --
    op.create_table(
        "folders",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("name", sa.String(256), nullable=False),
        sa.Column("parent_id", sa.Integer(), sa.ForeignKey("folders.id", ondelete="CASCADE"), nullable=True),
        sa.Column("owner_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("is_shared", sa.Boolean(), default=False),
        sa.Column("share_mode", sa.String(16), default="read_only"),
        sa.Column("created_at", sa.DateTime()),
        sa.Column("updated_at", sa.DateTime()),
    )

    # Add folder_id to assets (nullable, SET NULL on folder delete)
    op.add_column(
        "assets",
        sa.Column("folder_id", sa.Integer(), sa.ForeignKey("folders.id", ondelete="SET NULL"), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("assets", "folder_id")
    op.drop_table("folders")
