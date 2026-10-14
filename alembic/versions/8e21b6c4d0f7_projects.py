"""projects: cards for your own projects, read by the research agent

Revision ID: 8e21b6c4d0f7
Revises: 3c7d2a9e41b0
Create Date: 2026-10-10 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel


# revision identifiers, used by Alembic.
revision: str = '8e21b6c4d0f7'
down_revision: Union[str, None] = '3c7d2a9e41b0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('project',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('name', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('path', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('status', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('content_hash', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
    sa.Column('card', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
    sa.Column('card_source', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
    sa.Column('cost_usd', sa.Float(), nullable=True),
    sa.Column('scanned_at', sa.DateTime(), nullable=False),
    sa.Column('card_updated_at', sa.DateTime(), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_project_name'), 'project', ['name'], unique=True)


def downgrade() -> None:
    op.drop_index(op.f('ix_project_name'), table_name='project')
    op.drop_table('project')
