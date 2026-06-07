"""Billing tables: subscriptions, invoices, usage_records (Phase D)

Tenant-isolated (RLS) like the other per-practice tables. The clinic Billing tab
reads its own rows via the tenant session; the admin panel (Phase E) reads
cross-tenant through a separate, explicitly-audited code path.

Revision ID: o0j1k2l3m4n5
Revises: n9i0j1k2l3m4
Create Date: 2026-06-07
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = 'o0j1k2l3m4n5'
down_revision: str | None = 'n9i0j1k2l3m4'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _ts(name, **kw):
    return sa.Column(name, sa.DateTime(timezone=True), **kw)


def _rls(table: str) -> None:
    """Enable + force tenant-isolation RLS on a per-practice table."""
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"""
        CREATE POLICY tenant_isolation ON {table}
          USING (practice_id = NULLIF(current_setting('app.current_practice_id', true), '')::uuid)
        """
    )
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO dentiva_app")


def upgrade() -> None:
    op.create_table(
        'subscriptions',
        sa.Column('id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('practice_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('stripe_subscription_id', sa.Text(), nullable=True),
        sa.Column('plan', sa.Text(), nullable=False),
        sa.Column('billing_cycle', sa.Text(), nullable=False, server_default='monthly'),
        sa.Column('status', sa.Text(), nullable=False, server_default='trialing'),
        sa.Column('included_minutes', sa.Integer(), nullable=False),
        sa.Column('mrr_cents', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('overage_cents_per_min', sa.Integer(), nullable=True),
        sa.Column('setup_fee_cents', sa.Integer(), nullable=True),
        _ts('current_period_start', nullable=True),
        _ts('current_period_end', nullable=True),
        _ts('created_at', nullable=False, server_default=sa.func.now()),
        _ts('updated_at', nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(['practice_id'], ['practices.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_subscriptions_practice', 'subscriptions', ['practice_id'])
    _rls('subscriptions')

    op.create_table(
        'invoices',
        sa.Column('id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('practice_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('stripe_invoice_id', sa.Text(), nullable=True),
        sa.Column('amount_cents', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('status', sa.Text(), nullable=False, server_default='open'),
        _ts('period_start', nullable=True),
        _ts('period_end', nullable=True),
        _ts('paid_at', nullable=True),
        _ts('created_at', nullable=False, server_default=sa.func.now()),
        _ts('updated_at', nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(['practice_id'], ['practices.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_invoices_practice', 'invoices', ['practice_id', 'created_at'])
    _rls('invoices')

    op.create_table(
        'usage_records',
        sa.Column('id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('practice_id', postgresql.UUID(as_uuid=True), nullable=False),
        _ts('period_start', nullable=False),
        _ts('period_end', nullable=False),
        sa.Column('minutes_used', sa.Numeric(12, 2), nullable=False, server_default='0'),
        sa.Column('calls_count', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('sms_count', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('overage_minutes', sa.Numeric(12, 2), nullable=False, server_default='0'),
        _ts('created_at', nullable=False, server_default=sa.func.now()),
        _ts('updated_at', nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(['practice_id'], ['practices.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    # One usage row per practice per period — upserted on call_ended.
    op.create_index(
        'uq_usage_practice_period', 'usage_records',
        ['practice_id', 'period_start'], unique=True,
    )
    _rls('usage_records')


def downgrade() -> None:
    for table in ('usage_records', 'invoices', 'subscriptions'):
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")
    op.drop_index('uq_usage_practice_period', table_name='usage_records')
    op.drop_table('usage_records')
    op.drop_index('ix_invoices_practice', table_name='invoices')
    op.drop_table('invoices')
    op.drop_index('ix_subscriptions_practice', table_name='subscriptions')
    op.drop_table('subscriptions')
