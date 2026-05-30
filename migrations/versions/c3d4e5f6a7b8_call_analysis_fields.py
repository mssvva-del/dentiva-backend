"""Add call analysis fields

Revision ID: c3d4e5f6a7b8
Revises: b2c3d4e5f6a7
Create Date: 2026-05-30
"""
from alembic import op
import sqlalchemy as sa

revision = 'c3d4e5f6a7b8'
down_revision = 'b2c3d4e5f6a7'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('calls', sa.Column('call_intent', sa.Text(), nullable=True))
    op.add_column('calls', sa.Column('patient_sentiment', sa.Text(), nullable=True))
    op.add_column('calls', sa.Column('escalation_needed', sa.Boolean(), nullable=True))
    op.add_column('calls', sa.Column('hipaa_compliant', sa.Boolean(), nullable=True))


def downgrade() -> None:
    op.drop_column('calls', 'hipaa_compliant')
    op.drop_column('calls', 'escalation_needed')
    op.drop_column('calls', 'patient_sentiment')
    op.drop_column('calls', 'call_intent')
