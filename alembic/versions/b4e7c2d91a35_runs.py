"""runs: per-attempt traces and the cost ledger

Revision ID: b4e7c2d91a35
Revises: 8e21b6c4d0f7
Create Date: 2026-10-15 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel


# revision identifiers, used by Alembic.
revision: str = 'b4e7c2d91a35'
down_revision: Union[str, None] = '8e21b6c4d0f7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('run',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('action_id', sa.Integer(), nullable=True),
    sa.Column('video_id', sa.Integer(), nullable=True),
    sa.Column('kind', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('provider', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
    sa.Column('model', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
    sa.Column('status', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('error', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
    sa.Column('started_at', sa.DateTime(), nullable=False),
    sa.Column('duration_ms', sa.Integer(), nullable=True),
    sa.Column('cost_usd', sa.Float(), nullable=False),
    sa.Column('input_tokens', sa.Integer(), nullable=False),
    sa.Column('output_tokens', sa.Integer(), nullable=False),
    sa.Column('searches', sa.Integer(), nullable=False),
    sa.Column('steps', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_run_action_id'), 'run', ['action_id'], unique=False)
    op.create_index(op.f('ix_run_video_id'), 'run', ['video_id'], unique=False)
    op.create_index(op.f('ix_run_started_at'), 'run', ['started_at'], unique=False)
    # Seed the ledger with what earlier research already cost, so today's spend and
    # per-item totals don't reset to zero on upgrade.
    op.execute("INSERT INTO run (action_id, video_id, kind, status, started_at, cost_usd, input_tokens, "
               "output_tokens, searches, steps) SELECT id, video_id, 'research', 'ok', updated_at, cost_usd, 0, 0, 0, "
               "'[]' FROM action WHERE cost_usd IS NOT NULL AND cost_usd > 0")


def downgrade() -> None:
    op.drop_index(op.f('ix_run_started_at'), table_name='run')
    op.drop_index(op.f('ix_run_video_id'), table_name='run')
    op.drop_index(op.f('ix_run_action_id'), table_name='run')
    op.drop_table('run')
