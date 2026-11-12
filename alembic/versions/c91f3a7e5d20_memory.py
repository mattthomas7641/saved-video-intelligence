"""memory: embedded past reports for recall across shares

Revision ID: c91f3a7e5d20
Revises: b4e7c2d91a35
Create Date: 2026-10-15 18:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel


# revision identifiers, used by Alembic.
revision: str = 'c91f3a7e5d20'
down_revision: Union[str, None] = 'b4e7c2d91a35'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('memory',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('action_id', sa.Integer(), nullable=False),
    sa.Column('video_id', sa.Integer(), nullable=True),
    sa.Column('text', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('vector', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('model', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_memory_action_id'), 'memory', ['action_id'], unique=True)


def downgrade() -> None:
    op.drop_index(op.f('ix_memory_action_id'), table_name='memory')
    op.drop_table('memory')
