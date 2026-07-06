"""baa_acceptances — click-through Terms + BAA e-signatures (ONB-BAA)

Legal record of a clinic accepting a specific Terms+BAA version during
onboarding: signer name/title + server-captured IP/user-agent + timestamp. The
go-live gate refuses to activate a practice without a CURRENT-version acceptance.
Tenant-scoped (RLS FORCE) — a clinic sees only its own signatures.

Revision ID: e6z7a8b9c0d1
Revises: d5y6z7a8b9c0
Create Date: 2026-07-06
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = 'e6z7a8b9c0d1'
down_revision: str | None = 'd5y6z7a8b9c0'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        'baa_acceptances',
        sa.Column('id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('practice_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('document_version', sa.Text(), nullable=False),
        sa.Column('signer_name', sa.Text(), nullable=False),
        sa.Column('signer_title', sa.Text(), nullable=False),
        sa.Column('signer_ip', sa.Text(), nullable=True),
        sa.Column('user_agent', sa.Text(), nullable=True),
        sa.Column('accepted_by', postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.ForeignKeyConstraint(['practice_id'], ['practices.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    # UNIQUE: exactly one acceptance row per (practice, version) — makes the
    # click-through idempotent under concurrent submits (ON CONFLICT DO NOTHING)
    # and keeps the legal record clean (a new VERSION is a new row).
    op.create_index(
        'uq_baa_acceptances_practice_version', 'baa_acceptances',
        ['practice_id', 'document_version'], unique=True,
    )

    # Tenant isolation — same pattern as invitations/patients.
    op.execute("ALTER TABLE baa_acceptances ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE baa_acceptances FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY tenant_isolation ON baa_acceptances
          USING (
            practice_id = NULLIF(current_setting('app.current_practice_id', true), '')::uuid
          )
        """
    )
    op.execute(
        "GRANT SELECT, INSERT, UPDATE, DELETE ON baa_acceptances TO dentiva_app"
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON baa_acceptances")
    op.drop_index('uq_baa_acceptances_practice_version', table_name='baa_acceptances')
    op.drop_table('baa_acceptances')
