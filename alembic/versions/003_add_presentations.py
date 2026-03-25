"""Add presentations table and presentation_id to browser_sources

Revision ID: 003
Revises: 002
Create Date: 2026-03-25
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = "003"
down_revision: Union[str, None] = "002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "presentations",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("name", sa.String(512), nullable=False),
        sa.Column("owner_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("slide_count", sa.Integer(), default=0),
        sa.Column("current_slide", sa.Integer(), default=1),
        sa.Column("slides_dir", sa.String(1024), nullable=True),
        sa.Column("status", sa.String(16), default="uploading"),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime()),
        sa.Column("updated_at", sa.DateTime()),
    )

    # Link browser sources to presentations (optional, SET NULL on delete)
    op.add_column(
        "browser_sources",
        sa.Column("presentation_id", sa.Integer(),
                  sa.ForeignKey("presentations.id", ondelete="SET NULL"),
                  nullable=True),
    )


def downgrade() -> None:
    op.drop_column("browser_sources", "presentation_id")
    op.drop_table("presentations")
