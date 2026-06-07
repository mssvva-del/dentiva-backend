"""Add invitations table (team invitations, Phase B3)

Pending team invitations: an owner/manager invites an email with a clinic role;
when the person accepts (Clerk org invitation → membership), the webhook marks it
accepted and assigns the invited role. Tenant-isolated (RLS) like patients —
staff emails shouldn't leak across practices.

Revision ID: n9i0j1k2l3m4
Revises: m8h9i0j1k2l3
Create Date: 2026-06-07
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = 'n9i0j1k2l3m4'
down_revision: str | None = 'm8h9i0j1k2l3'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        'invitations',
        sa.Column('id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('practice_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('email', sa.Text(), nullable=False),
        sa.Column('role', sa.Text(), nullable=False),
        # pending | accepted | revoked | expired
        sa.Column('status', sa.Text(), nullable=False, server_default='pending'),
        sa.Column('clerk_invitation_id', sa.Text(), nullable=True),
        sa.Column('invited_by', postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.ForeignKeyConstraint(['practice_id'], ['practices.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        'ix_invitations_practice_status', 'invitations', ['practice_id', 'status']
    )
    # One live invite per (practice, email): partial unique on pending rows so a
    # re-invite after revoke/accept is allowed.
    op.execute(
        "CREATE UNIQUE INDEX uq_invitations_pending_email ON invitations "
        "(practice_id, lower(email)) WHERE status = 'pending'"
    )

    # Tenant isolation — same pattern as patients/callback_requests.
    op.execute("ALTER TABLE invitations ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE invitations FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY tenant_isolation ON invitations
          USING (
            practice_id = NULLIF(current_setting('app.current_practice_id', true), '')::uuid
          )
        """
    )
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON invitations TO dentiva_app")


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON invitations")
    op.execute("ALTER TABLE invitations NO FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE invitations DISABLE ROW LEVEL SECURITY")
    op.execute("DROP INDEX IF EXISTS uq_invitations_pending_email")
    op.drop_index('ix_invitations_practice_status', table_name='invitations')
    op.drop_table('invitations')
