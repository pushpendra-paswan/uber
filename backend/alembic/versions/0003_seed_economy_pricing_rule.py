"""seed economy pricing rule

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-08 13:08:21.115090

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0003'
down_revision: Union[str, Sequence[str], None] = '0002'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Data only, no schema change. All money values are in paise."""
    pricing_rules = sa.table(
        "pricing_rules",
        sa.column("vehicle_type", sa.String),
        sa.column("base_fare", sa.Integer),
        sa.column("per_km", sa.Integer),
        sa.column("per_min", sa.Integer),
        sa.column("min_fare", sa.Integer),
        sa.column("surge_cap", sa.Float),
    )
    op.bulk_insert(
        pricing_rules,
        [{"vehicle_type": "economy", "base_fare": 5000, "per_km": 1200, "per_min": 200, "min_fare": 8000, "surge_cap": 2.0}],
    )


def downgrade() -> None:
    """Remove the seeded row."""
    op.execute("DELETE FROM pricing_rules WHERE vehicle_type = 'economy'")
