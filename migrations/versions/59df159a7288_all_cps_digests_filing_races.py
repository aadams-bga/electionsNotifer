"""all_cps flag, digest prefs, filing_races, digest_sends

Revision ID: 59df159a7288
Revises: 14890def496a
Create Date: 2026-06-12

"""
from alembic import op
import sqlalchemy as sa


revision = '59df159a7288'
down_revision = '14890def496a'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        'subscriptions',
        sa.Column('all_cps', sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        'subscribers',
        sa.Column('wants_daily_digest', sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        'subscribers',
        sa.Column('wants_weekly_digest', sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.create_table(
        'filing_races',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('filing_id', sa.Integer(), nullable=False),
        sa.Column('race_id', sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(['filing_id'], ['filings.id']),
        sa.ForeignKeyConstraint(['race_id'], ['races.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('filing_id', 'race_id'),
    )
    op.create_index(op.f('ix_filing_races_filing_id'), 'filing_races', ['filing_id'])
    op.create_index(op.f('ix_filing_races_race_id'), 'filing_races', ['race_id'])
    op.create_table(
        'digest_sends',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('subscriber_id', sa.Integer(), nullable=False),
        sa.Column('kind', sa.String(length=10), nullable=False),
        sa.Column('period_start', sa.Date(), nullable=False),
        sa.Column('status', sa.String(length=20), nullable=False),
        sa.Column('detail', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('sent_at', sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(['subscriber_id'], ['subscribers.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('subscriber_id', 'kind', 'period_start', name='uq_digest_once'),
    )
    op.create_index(op.f('ix_digest_sends_subscriber_id'), 'digest_sends', ['subscriber_id'])


def downgrade() -> None:
    op.drop_table('digest_sends')
    op.drop_table('filing_races')
    op.drop_column('subscribers', 'wants_weekly_digest')
    op.drop_column('subscribers', 'wants_daily_digest')
    op.drop_column('subscriptions', 'all_cps')
