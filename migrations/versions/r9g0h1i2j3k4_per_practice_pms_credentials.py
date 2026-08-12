"""practices.pms_credentials — each clinic's own bridge, instead of the deployment's

NEXHEALTH_* and KOLLA_* in the environment name ONE clinic's location or linked
account. Every practice that had picked a PMS used them, so the second clinic to
finish onboarding would have had its agent read the FIRST clinic's openings aloud
and, with writes on, book its patients into the first clinic's chairs. Nothing
would have errored; the two clinics would simply have been one clinic.

PMS_ENV_PRACTICE_ID closed the dangerous half of that by binding the environment
to one named practice — but it left the second clinic with no PMS at all, and no
way to give it one short of a redeploy. This column is that way.

Encrypted at rest (Fernet, like the PHI columns): the value is a live API key
into a dental practice's own system, which is a larger thing to leak than any one
patient record. It is therefore write-only from the API's side — set through the
admin endpoint, never read back out.

nullable, no default: NULL means "use the environment, if the environment is
bound to me", which is every clinic today.

Revision ID: r9g0h1i2j3k4
Revises: q8f9a0b1c2d3
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "r9g0h1i2j3k4"
down_revision = "q8f9a0b1c2d3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "practices",
        sa.Column("pms_credentials", sa.LargeBinary(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("practices", "pms_credentials")
