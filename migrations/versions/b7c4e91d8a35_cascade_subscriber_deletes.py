"""ON DELETE CASCADE for every subscriber_id foreign key

/unsubscribe deletes the Subscriber row, but only subscriptions and
push_subscriptions were cleaned up with it. notifications and digest_sends were
not, so the delete hit a foreign-key violation and returned 500 for anyone who
had ever been sent an alert or a digest — 112 of 130 live subscribers when this
was found, including everyone receiving the weekly summary whose footer carries
the unsubscribe link.

Fixing it in the database rather than only in the ORM means the invariant holds
however the row is deleted, including by hand in psql.

Revision ID: b7c4e91d8a35
Revises: a3f1c07b52e4
Create Date: 2026-10-13
"""

from alembic import op

revision = "b7c4e91d8a35"
down_revision = "a3f1c07b52e4"
branch_labels = None
depends_on = None

TABLES = ("subscriptions", "push_subscriptions", "notifications", "digest_sends")

# Postgres auto-names these constraints; SQLite leaves them unnamed, so batch
# mode needs the convention spelled out to match the reflected constraint.
NAMING = {"fk": "%(table_name)s_%(column_0_name)s_fkey"}


def upgrade() -> None:
    for table in TABLES:
        with op.batch_alter_table(table, naming_convention=NAMING) as batch:
            batch.drop_constraint(f"{table}_subscriber_id_fkey", type_="foreignkey")
            batch.create_foreign_key(
                f"{table}_subscriber_id_fkey",
                "subscribers",
                ["subscriber_id"],
                ["id"],
                ondelete="CASCADE",
            )


def downgrade() -> None:
    for table in TABLES:
        with op.batch_alter_table(table, naming_convention=NAMING) as batch:
            batch.drop_constraint(f"{table}_subscriber_id_fkey", type_="foreignkey")
            batch.create_foreign_key(
                f"{table}_subscriber_id_fkey",
                "subscribers",
                ["subscriber_id"],
                ["id"],
            )
