"""Add presentation source type and file_path to presentations

Revision ID: 004
Revises: 003
Create Date: 2026-03-25
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = "004"
down_revision: Union[str, None] = "003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Add 'presentation' to the streamsourcetype PostgreSQL enum.
    # This must be done outside a transaction block for PostgreSQL enums.
    op.execute("ALTER TYPE streamsourcetype ADD VALUE IF NOT EXISTS 'presentation'")

    # Add file_path column to presentations for storing the raw upload path
    op.add_column(
        "presentations",
        sa.Column("file_path", sa.String(1024), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("presentations", "file_path")
    # Note: PostgreSQL does not support removing enum values.
    # The 'presentation' value will remain in the enum but be unused.
