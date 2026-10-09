"""surge zones

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-09 11:10:48.957391

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0007'
down_revision: Union[str, Sequence[str], None] = '0006'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Existing rides get surge_percent 100 and a NULL zone: no data statement.
    op.add_column('rides', sa.Column('pickup_zone', sa.String(length=12), nullable=True))
    op.add_column('rides', sa.Column('surge_percent', sa.Integer(), server_default='100', nullable=False))
    # Alembic does not detect check constraints, so this one is written by hand. The full name is written out, as in 0001
    # (ck_rides_surge_percent_range).
    op.create_check_constraint(op.f('ck_rides_surge_percent_range'), 'rides', 'surge_percent BETWEEN 100 AND 200')
    op.create_index('ix_rides_created_at', 'rides', ['created_at'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('ix_rides_created_at', table_name='rides')
    op.drop_constraint(op.f('ck_rides_surge_percent_range'), 'rides', type_='check')
    op.drop_column('rides', 'surge_percent')
    op.drop_column('rides', 'pickup_zone')
