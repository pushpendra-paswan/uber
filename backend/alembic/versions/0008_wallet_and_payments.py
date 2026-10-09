"""wallet and payments

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-09 15:41:36.878671

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0008'
down_revision: Union[str, Sequence[str], None] = '0007'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('stripe_events',
    sa.Column('id', sa.String(length=255), nullable=False),
    sa.Column('event_type', sa.String(length=100), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_stripe_events'))
    )
    op.create_table('wallet_topups',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('amount', sa.Integer(), nullable=False),
    sa.Column('status', sa.Enum('PENDING', 'SUCCEEDED', 'EXPIRED', name='topupstatus', native_enum=False), server_default='PENDING', nullable=False),
    sa.Column('idempotency_key', sa.String(length=64), nullable=False),
    sa.Column('stripe_session_id', sa.String(length=255), nullable=True),
    sa.Column('stripe_payment_intent_id', sa.String(length=255), nullable=True),
    sa.Column('checkout_url', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('completed_at', sa.DateTime(timezone=True), nullable=True),
    sa.CheckConstraint('amount > 0', name=op.f('ck_wallet_topups_amount_positive')),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_wallet_topups_user_id_users')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_wallet_topups')),
    sa.UniqueConstraint('stripe_payment_intent_id', name=op.f('uq_wallet_topups_stripe_payment_intent_id')),
    sa.UniqueConstraint('stripe_session_id', name=op.f('uq_wallet_topups_stripe_session_id')),
    sa.UniqueConstraint('user_id', 'idempotency_key', name='uq_wallet_topups_user_id_idempotency_key')
    )
    op.create_index('ix_wallet_topups_user_id_id', 'wallet_topups', ['user_id', 'id'], unique=False)
    op.create_table('wallets',
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('balance', sa.Integer(), server_default='0', nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_wallets_user_id_users')),
    sa.PrimaryKeyConstraint('user_id', name=op.f('pk_wallets'))
    )
    op.create_table('wallet_entries',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('amount', sa.Integer(), nullable=False),
    sa.Column('kind', sa.Enum('TOPUP', 'RIDE_CHARGE', 'ADJUSTMENT', name='walletentrykind', native_enum=False), nullable=False),
    sa.Column('balance_after', sa.Integer(), nullable=False),
    sa.Column('ride_id', sa.Integer(), nullable=True),
    sa.Column('topup_id', sa.Integer(), nullable=True),
    sa.Column('idempotency_key', sa.String(length=64), nullable=True),
    sa.Column('note', sa.String(length=200), nullable=True),
    sa.Column('actor_user_id', sa.Integer(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("(kind <> 'RIDE_CHARGE' OR amount < 0) AND (kind <> 'TOPUP' OR amount > 0)", name=op.f('ck_wallet_entries_amount_sign_matches_kind')),
    sa.CheckConstraint("(kind = 'RIDE_CHARGE') = (ride_id IS NOT NULL)", name=op.f('ck_wallet_entries_ride_id_matches_kind')),
    sa.CheckConstraint("(kind = 'TOPUP') = (topup_id IS NOT NULL)", name=op.f('ck_wallet_entries_topup_id_matches_kind')),
    sa.CheckConstraint('amount <> 0', name=op.f('ck_wallet_entries_amount_nonzero')),
    sa.ForeignKeyConstraint(['actor_user_id'], ['users.id'], name=op.f('fk_wallet_entries_actor_user_id_users')),
    sa.ForeignKeyConstraint(['ride_id'], ['rides.id'], name=op.f('fk_wallet_entries_ride_id_rides')),
    sa.ForeignKeyConstraint(['topup_id'], ['wallet_topups.id'], name=op.f('fk_wallet_entries_topup_id_wallet_topups')),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_wallet_entries_user_id_users')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_wallet_entries')),
    sa.UniqueConstraint('user_id', 'idempotency_key', name='uq_wallet_entries_user_id_idempotency_key')
    )
    op.create_index('ix_wallet_entries_user_id_id', 'wallet_entries', ['user_id', 'id'], unique=False)
    op.create_index('uq_wallet_entries_one_charge_per_ride', 'wallet_entries', ['ride_id'], unique=True, postgresql_where=sa.text("kind = 'RIDE_CHARGE'"))
    op.create_index('uq_wallet_entries_one_credit_per_topup', 'wallet_entries', ['topup_id'], unique=True, postgresql_where=sa.text("kind = 'TOPUP'"))
    op.drop_index(op.f('ix_payments_ride_id'), table_name='payments')
    op.create_unique_constraint(op.f('uq_payments_ride_id'), 'payments', ['ride_id'])
    op.add_column('rides', sa.Column('payment_method', sa.Enum('cash', 'wallet', 'card', name='paymentmethod', native_enum=False), server_default='cash', nullable=False))
    # Alembic does not detect check constraints on an existing table, so this one is written by hand (full name, as in 0007).
    op.create_check_constraint(op.f('ck_payments_amount_positive'), 'payments', 'amount > 0')
    # Every ride settled before this milestone with something to pay gets its payment, so the invariants hold for old rides
    # too (the backslash keeps text() from reading \:charge as a bind parameter). They were all cash (the only method that existed). Rides marked legacy have no fare and are left out.
    op.execute("""
        INSERT INTO payments (ride_id, amount, method, status, idempotency_key, created_at)
        SELECT id, final_fare, 'cash', 'succeeded', 'ride:' || id || '\\:charge', COALESCE(completed_at, created_at)
        FROM rides
        WHERE status IN ('COMPLETED', 'CANCELLED') AND final_fare > 0 AND COALESCE(fare_breakdown->>'kind', '') <> 'legacy'
    """)


def downgrade() -> None:
    """Downgrade schema."""
    # The backfilled payments go first (upgrade re-creates them). Payments made after this milestone have the same key
    # pattern and go too: the table returns to the shape where nothing writes it.
    op.execute("DELETE FROM payments WHERE idempotency_key LIKE 'ride:%\\:charge'")
    op.drop_column('rides', 'payment_method')
    op.drop_constraint(op.f('ck_payments_amount_positive'), 'payments', type_='check')
    op.drop_constraint(op.f('uq_payments_ride_id'), 'payments', type_='unique')
    op.create_index(op.f('ix_payments_ride_id'), 'payments', ['ride_id'], unique=False)
    op.drop_index('uq_wallet_entries_one_credit_per_topup', table_name='wallet_entries', postgresql_where=sa.text("kind = 'TOPUP'"))
    op.drop_index('uq_wallet_entries_one_charge_per_ride', table_name='wallet_entries', postgresql_where=sa.text("kind = 'RIDE_CHARGE'"))
    op.drop_index('ix_wallet_entries_user_id_id', table_name='wallet_entries')
    op.drop_table('wallet_entries')
    op.drop_table('wallets')
    op.drop_index('ix_wallet_topups_user_id_id', table_name='wallet_topups')
    op.drop_table('wallet_topups')
    op.drop_table('stripe_events')
