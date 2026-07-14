"""Subscriber consent: terms_accepted_at + marketing_opt_in

Revision ID: c41d20aa77e3
Revises: 59df159a7288
Create Date: 2026-07-14
"""

import sqlalchemy as sa
from alembic import op

revision = "c41d20aa77e3"
down_revision = "59df159a7288"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "subscribers", sa.Column("terms_accepted_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "subscribers",
        sa.Column("marketing_opt_in", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column("subscribers", "marketing_opt_in")
    op.drop_column("subscribers", "terms_accepted_at")
