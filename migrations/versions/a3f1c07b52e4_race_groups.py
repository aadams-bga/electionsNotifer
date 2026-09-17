"""Race groups: races.race_group + subscriptions.all_group

Adds the grouping that lets the races table hold more than CPS races.
server_default="cps" backfills the 21 existing rows correctly; the default is
then dropped so new rows must state their group explicitly.

subscriptions.all_group is the "follow every race in this group" opt-in for the
new groups. The existing all_cps boolean is deliberately left alone so live
subscriptions need no data migration.

Revision ID: a3f1c07b52e4
Revises: c41d20aa77e3
Create Date: 2026-09-16
"""

import sqlalchemy as sa
from alembic import op

revision = "a3f1c07b52e4"
down_revision = "c41d20aa77e3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "races",
        sa.Column("race_group", sa.String(length=20), nullable=False, server_default="cps"),
    )
    op.create_index("ix_races_race_group", "races", ["race_group"])
    # Existing rows are all CPS and are now backfilled; stop defaulting so a
    # future race can't silently land in the CPS group.
    with op.batch_alter_table("races") as batch:
        batch.alter_column("race_group", server_default=None)

    op.add_column(
        "subscriptions", sa.Column("all_group", sa.String(length=20), nullable=True)
    )
    op.create_index("ix_subscriptions_all_group", "subscriptions", ["all_group"])


def downgrade() -> None:
    op.drop_index("ix_subscriptions_all_group", table_name="subscriptions")
    op.drop_column("subscriptions", "all_group")
    op.drop_index("ix_races_race_group", table_name="races")
    op.drop_column("races", "race_group")
