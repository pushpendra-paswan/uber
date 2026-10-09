from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Payment, Ride, RideEarning


async def create(
    db: AsyncSession, ride_id: int, payment_id: int, driver_id: int, kind: str, gross_amount: int, commission_percent: int,
    platform_fee: int, driver_earning: int,
) -> None:
    db.add(
        RideEarning(
            ride_id=ride_id, payment_id=payment_id, driver_id=driver_id, kind=kind, gross_amount=gross_amount,
            commission_percent=commission_percent, platform_fee=platform_fee, driver_earning=driver_earning,
        )
    )
    await db.flush()


async def summarize(db: AsyncSession, since: datetime | None, until: datetime | None, driver_id: int | None = None) -> list:
    """ONE grouped query, so the totals of an answer always agree with each other. Rows of (kind, method, rides, gross,
    platform_fee, driver_earning) for the earnings settled from `since` (inclusive) until `until` (exclusive)."""
    query = (
        select(
            RideEarning.kind,
            Payment.method.label("method"),
            func.count().label("rides"),
            func.sum(RideEarning.gross_amount).label("gross"),
            func.sum(RideEarning.platform_fee).label("platform_fee"),
            func.sum(RideEarning.driver_earning).label("driver_earning"),
        )
        .join(Payment, Payment.id == RideEarning.payment_id)
        .group_by(RideEarning.kind, Payment.method)
    )
    if driver_id is not None:
        query = query.where(RideEarning.driver_id == driver_id)
    if since is not None:
        query = query.where(RideEarning.created_at >= since)
    if until is not None:
        query = query.where(RideEarning.created_at < until)
    result = await db.execute(query)
    return list(result.all())


async def list_for_driver(
    db: AsyncSession, driver_id: int, since: datetime | None, until: datetime | None, limit: int, before_id: int | None
) -> list[dict]:
    query = (
        select(
            RideEarning.id, RideEarning.ride_id, RideEarning.kind, Payment.method.label("payment_method"),
            RideEarning.gross_amount, RideEarning.commission_percent, RideEarning.platform_fee, RideEarning.driver_earning,
            Ride.pickup_address, Ride.dropoff_address, Ride.actual_distance_m.label("distance_m"),
            Ride.actual_duration_s.label("duration_s"), RideEarning.created_at,
        )
        .join(Payment, Payment.id == RideEarning.payment_id)
        .join(Ride, Ride.id == RideEarning.ride_id)
        .where(RideEarning.driver_id == driver_id)
    )
    if since is not None:
        query = query.where(RideEarning.created_at >= since)
    if until is not None:
        query = query.where(RideEarning.created_at < until)
    if before_id is not None:
        query = query.where(RideEarning.id < before_id)
    result = await db.execute(query.order_by(RideEarning.id.desc()).limit(limit))
    return [dict(row) for row in result.mappings().all()]
