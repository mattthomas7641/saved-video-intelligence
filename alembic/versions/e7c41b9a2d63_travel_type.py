"""travel action type: flight itineraries, flight and travel deals, trips

Revision ID: e7c41b9a2d63
Revises: d2a8e61f0b47
Create Date: 2026-11-20 16:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e7c41b9a2d63'
down_revision: Union[str, None] = 'd2a8e61f0b47'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_OLD = sa.Enum('SKILL', 'PROJECT', 'JOB', 'REPO', 'RESEARCH', 'PLACE', 'OTHER', name='actiontype')
_NEW = sa.Enum('SKILL', 'PROJECT', 'JOB', 'REPO', 'RESEARCH', 'PLACE', 'TRAVEL', 'OTHER', name='actiontype')


def upgrade() -> None:
    with op.batch_alter_table('action') as batch:
        batch.alter_column('action_type', existing_type=_OLD, type_=_NEW, existing_nullable=False)


def downgrade() -> None:
    op.execute("UPDATE action SET action_type = 'OTHER' WHERE action_type = 'TRAVEL'")
    with op.batch_alter_table('action') as batch:
        batch.alter_column('action_type', existing_type=_NEW, type_=_OLD, existing_nullable=False)
