"""telegram intake + research agent: video.source, action verdict/cost/telegram ids, new action types

Revision ID: 3c7d2a9e41b0
Revises: fb1f58bec396
Create Date: 2026-10-09 15:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel


# revision identifiers, used by Alembic.
revision: str = '3c7d2a9e41b0'
down_revision: Union[str, None] = 'fb1f58bec396'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_OLD_TYPES = sa.Enum('SKILL', 'PROJECT', 'JOB', name='actiontype')
_NEW_TYPES = sa.Enum('SKILL', 'PROJECT', 'JOB', 'REPO', 'RESEARCH', 'PLACE', 'OTHER', name='actiontype')


def upgrade() -> None:
    with op.batch_alter_table('video') as batch:
        batch.add_column(sa.Column('source', sqlmodel.sql.sqltypes.AutoString(), nullable=True))
    with op.batch_alter_table('action') as batch:
        batch.alter_column('action_type', existing_type=_OLD_TYPES, type_=_NEW_TYPES, existing_nullable=False)
        batch.add_column(sa.Column('verdict', sqlmodel.sql.sqltypes.AutoString(), nullable=True))
        batch.add_column(sa.Column('cost_usd', sa.Float(), nullable=True))
        batch.add_column(sa.Column('telegram_chat_id', sa.BigInteger(), nullable=True))
        batch.add_column(sa.Column('telegram_message_id', sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('action') as batch:
        batch.drop_column('telegram_message_id')
        batch.drop_column('telegram_chat_id')
        batch.drop_column('cost_usd')
        batch.drop_column('verdict')
        batch.alter_column('action_type', existing_type=_NEW_TYPES, type_=_OLD_TYPES, existing_nullable=False)
    with op.batch_alter_table('video') as batch:
        batch.drop_column('source')
