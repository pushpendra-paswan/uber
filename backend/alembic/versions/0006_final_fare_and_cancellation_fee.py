"""final fare and cancellation fee

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-09 09:33:37.595682

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '0006'
down_revision: Union[str, Sequence[str], None] = '0005'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('pricing_rules', sa.Column('cancellation_fee', sa.Integer(), server_default='3000', nullable=False))
    op.add_column('pricing_rules', sa.Column('free_cancel_seconds', sa.Integer(), server_default='120', nullable=False))
    op.add_column('rides', sa.Column('actual_distance_m', sa.Integer(), nullable=True))
    op.add_column('rides', sa.Column('actual_duration_s', sa.Integer(), nullable=True))
    op.add_column('rides', sa.Column('fare_breakdown', postgresql.JSONB(astext_type=sa.Text()), nullable=True))
    # Rides settled before this milestone have no fare. The marker lets the invariants and later receipts tell them apart.
    op.execute("""UPDATE rides SET fare_breakdown = '{"kind": "legacy"}'::jsonb WHERE status IN ('COMPLETED', 'CANCELLED')""")


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('rides', 'fare_breakdown')
    op.drop_column('rides', 'actual_duration_s')
    op.drop_column('rides', 'actual_distance_m')
    op.drop_column('pricing_rules', 'free_cancel_seconds')
    op.drop_column('pricing_rules', 'cancellation_fee')
