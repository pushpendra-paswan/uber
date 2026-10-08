"""initial schema

Revision ID: 0001
Revises: 
Create Date: 2026-10-08 11:59:04.569940

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0001'
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS postgis")
    op.create_table('pricing_rules',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('vehicle_type', sa.String(length=30), nullable=False),
    sa.Column('base_fare', sa.Integer(), nullable=False),
    sa.Column('per_km', sa.Integer(), nullable=False),
    sa.Column('per_min', sa.Integer(), nullable=False),
    sa.Column('min_fare', sa.Integer(), nullable=False),
    sa.Column('surge_cap', sa.Float(), server_default='2.0', nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_pricing_rules')),
    sa.UniqueConstraint('vehicle_type', name=op.f('uq_pricing_rules_vehicle_type'))
    )
    op.create_table('users',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('role', sa.Enum('rider', 'driver', 'admin', name='userrole', native_enum=False), nullable=False),
    sa.Column('name', sa.String(length=100), nullable=False),
    sa.Column('email', sa.String(length=255), nullable=False),
    sa.Column('phone', sa.String(length=20), nullable=True),
    sa.Column('password_hash', sa.String(length=255), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_users')),
    sa.UniqueConstraint('email', name=op.f('uq_users_email')),
    sa.UniqueConstraint('phone', name=op.f('uq_users_phone'))
    )
    op.create_table('drivers',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('license_number', sa.String(length=50), nullable=False),
    sa.Column('verification_status', sa.Enum('pending', 'approved', 'rejected', name='verificationstatus', native_enum=False), server_default='pending', nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_drivers_user_id_users')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_drivers')),
    sa.UniqueConstraint('user_id', name=op.f('uq_drivers_user_id'))
    )
    op.create_table('rides',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('rider_id', sa.Integer(), nullable=False),
    sa.Column('driver_id', sa.Integer(), nullable=True),
    sa.Column('pickup_lat', sa.Float(), nullable=False),
    sa.Column('pickup_lng', sa.Float(), nullable=False),
    sa.Column('pickup_address', sa.String(length=255), nullable=False),
    sa.Column('dropoff_lat', sa.Float(), nullable=False),
    sa.Column('dropoff_lng', sa.Float(), nullable=False),
    sa.Column('dropoff_address', sa.String(length=255), nullable=False),
    sa.Column('status', sa.Enum('REQUESTED', 'DRIVER_ASSIGNED', 'DRIVER_ARRIVED', 'IN_PROGRESS', 'COMPLETED', 'CANCELLED', 'NO_DRIVER_FOUND', name='ridestatus', native_enum=False), server_default='REQUESTED', nullable=False),
    sa.Column('distance_m', sa.Integer(), nullable=True),
    sa.Column('duration_s', sa.Integer(), nullable=True),
    sa.Column('fare_estimate', sa.Integer(), nullable=True),
    sa.Column('final_fare', sa.Integer(), nullable=True),
    sa.Column('otp', sa.String(length=4), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('started_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('completed_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['driver_id'], ['drivers.id'], name=op.f('fk_rides_driver_id_drivers')),
    sa.ForeignKeyConstraint(['rider_id'], ['users.id'], name=op.f('fk_rides_rider_id_users')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_rides'))
    )
    op.create_index(op.f('ix_rides_driver_id'), 'rides', ['driver_id'], unique=False)
    op.create_index(op.f('ix_rides_rider_id'), 'rides', ['rider_id'], unique=False)
    op.create_index(op.f('ix_rides_status'), 'rides', ['status'], unique=False)
    op.create_table('vehicles',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('driver_id', sa.Integer(), nullable=False),
    sa.Column('plate_number', sa.String(length=20), nullable=False),
    sa.Column('model', sa.String(length=50), nullable=False),
    sa.Column('color', sa.String(length=30), nullable=False),
    sa.Column('vehicle_type', sa.String(length=30), server_default='economy', nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['driver_id'], ['drivers.id'], name=op.f('fk_vehicles_driver_id_drivers')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_vehicles')),
    sa.UniqueConstraint('plate_number', name=op.f('uq_vehicles_plate_number'))
    )
    op.create_table('payments',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('ride_id', sa.Integer(), nullable=False),
    sa.Column('amount', sa.Integer(), nullable=False),
    sa.Column('method', sa.Enum('cash', 'wallet', 'card', name='paymentmethod', native_enum=False), nullable=False),
    sa.Column('status', sa.Enum('pending', 'succeeded', 'failed', 'refunded', name='paymentstatus', native_enum=False), server_default='pending', nullable=False),
    sa.Column('idempotency_key', sa.String(length=100), nullable=False),
    sa.Column('gateway_ref', sa.String(length=100), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['ride_id'], ['rides.id'], name=op.f('fk_payments_ride_id_rides')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_payments')),
    sa.UniqueConstraint('gateway_ref', name=op.f('uq_payments_gateway_ref')),
    sa.UniqueConstraint('idempotency_key', name=op.f('uq_payments_idempotency_key'))
    )
    op.create_index(op.f('ix_payments_ride_id'), 'payments', ['ride_id'], unique=False)
    op.create_table('ratings',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('ride_id', sa.Integer(), nullable=False),
    sa.Column('from_user_id', sa.Integer(), nullable=False),
    sa.Column('to_user_id', sa.Integer(), nullable=False),
    sa.Column('score', sa.Integer(), nullable=False),
    sa.Column('comment', sa.String(length=500), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint('score >= 1 AND score <= 5', name=op.f('ck_ratings_score_range')),
    sa.ForeignKeyConstraint(['from_user_id'], ['users.id'], name=op.f('fk_ratings_from_user_id_users')),
    sa.ForeignKeyConstraint(['ride_id'], ['rides.id'], name=op.f('fk_ratings_ride_id_rides')),
    sa.ForeignKeyConstraint(['to_user_id'], ['users.id'], name=op.f('fk_ratings_to_user_id_users')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_ratings')),
    sa.UniqueConstraint('ride_id', 'from_user_id', name='uq_ratings_ride_id_from_user_id')
    )
    op.create_table('ride_events',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('ride_id', sa.Integer(), nullable=False),
    sa.Column('from_status', sa.Enum('REQUESTED', 'DRIVER_ASSIGNED', 'DRIVER_ARRIVED', 'IN_PROGRESS', 'COMPLETED', 'CANCELLED', 'NO_DRIVER_FOUND', name='ridestatus', native_enum=False), nullable=True),
    sa.Column('to_status', sa.Enum('REQUESTED', 'DRIVER_ASSIGNED', 'DRIVER_ARRIVED', 'IN_PROGRESS', 'COMPLETED', 'CANCELLED', 'NO_DRIVER_FOUND', name='ridestatus', native_enum=False), nullable=False),
    sa.Column('actor_user_id', sa.Integer(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['actor_user_id'], ['users.id'], name=op.f('fk_ride_events_actor_user_id_users')),
    sa.ForeignKeyConstraint(['ride_id'], ['rides.id'], name=op.f('fk_ride_events_ride_id_rides')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_ride_events'))
    )
    op.create_index(op.f('ix_ride_events_ride_id'), 'ride_events', ['ride_id'], unique=False)


def downgrade() -> None:
    # The postgis extension is left in place on purpose.
    op.drop_index(op.f('ix_ride_events_ride_id'), table_name='ride_events')
    op.drop_table('ride_events')
    op.drop_table('ratings')
    op.drop_index(op.f('ix_payments_ride_id'), table_name='payments')
    op.drop_table('payments')
    op.drop_table('vehicles')
    op.drop_index(op.f('ix_rides_status'), table_name='rides')
    op.drop_index(op.f('ix_rides_rider_id'), table_name='rides')
    op.drop_index(op.f('ix_rides_driver_id'), table_name='rides')
    op.drop_table('rides')
    op.drop_table('drivers')
    op.drop_table('users')
    op.drop_table('pricing_rules')
