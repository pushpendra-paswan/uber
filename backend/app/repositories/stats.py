from datetime import datetime

from sqlalchemy import Numeric, cast, extract, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import OfferStatus, Ride, RideEarning, RideEvent, RideOffer, RideStatus, User

# Read-only queries for GET /admin/stats (M6.2). Every window is [since, until): since inclusive, until exclusive.
# Ride numbers use rides.created_at, money uses ride_earnings.created_at (settlement time). No locks, no rules.


async def count_rides_by_status(db: AsyncSession, since: datetime, until: datetime) -> dict[RideStatus, int]:
    result = await db.execute(
        select(Ride.status, func.count()).where(Ride.created_at >= since, Ride.created_at < until).group_by(Ride.status)
    )
    return {status: count for status, count in result.all()}


async def trip_averages(db: AsyncSession, since: datetime, until: datetime):
    """Counts and sums over COMPLETED rides created in the window that have a final fare. Each column has its own count,
    so a ride settled before M5.1 (no distance or duration) cannot pull an average down."""
    result = await db.execute(
        select(
            func.count(Ride.final_fare).label("fares"), func.coalesce(func.sum(Ride.final_fare), 0).label("fare_total"),
            func.count(Ride.actual_distance_m).label("distances"), func.coalesce(func.sum(Ride.actual_distance_m), 0).label("distance_total"),
            func.count(Ride.actual_duration_s).label("durations"), func.coalesce(func.sum(Ride.actual_duration_s), 0).label("duration_total"),
        )
        .where(Ride.status == RideStatus.COMPLETED, Ride.final_fare.is_not(None), Ride.created_at >= since, Ride.created_at < until)
    )
    return result.one()


async def assignment_times(db: AsyncSession, since: datetime, until: datetime):
    """(rides, mean_s, median_s) of DRIVER_ASSIGNED event time minus ride creation time, for rides created in the window.
    A ride is assigned at most once, so the join gives one row per assigned ride. Seconds are exact numerics."""
    wait = RideEvent.created_at - Ride.created_at
    result = await db.execute(
        select(
            func.count().label("rides"),
            cast(extract("epoch", func.avg(wait)), Numeric).label("mean_s"),
            cast(extract("epoch", func.percentile_cont(0.5).within_group(wait)), Numeric).label("median_s"),
        )
        .select_from(Ride)
        .join(RideEvent, (RideEvent.ride_id == Ride.id) & (RideEvent.to_status == RideStatus.DRIVER_ASSIGNED))
        .where(Ride.created_at >= since, Ride.created_at < until)
    )
    return result.one()


async def count_offers_by_status(db: AsyncSession, since: datetime, until: datetime) -> dict[OfferStatus, int]:
    result = await db.execute(
        select(RideOffer.status, func.count()).where(RideOffer.created_at >= since, RideOffer.created_at < until).group_by(RideOffer.status)
    )
    return {status: count for status, count in result.all()}


async def surge_summary(db: AsyncSession, since: datetime, until: datetime):
    """(rides_surged, max_surge_percent or None) over rides created in the window with a multiplier above 100."""
    result = await db.execute(
        select(func.count().label("rides_surged"), func.max(Ride.surge_percent).label("max_surge_percent"))
        .where(Ride.surge_percent > 100, Ride.created_at >= since, Ride.created_at < until)
    )
    return result.one()


async def count_new_users(db: AsyncSession, since: datetime, until: datetime) -> dict:
    result = await db.execute(
        select(User.role, func.count()).where(User.created_at >= since, User.created_at < until).group_by(User.role)
    )
    return {role.value: count for role, count in result.all()}


async def ride_series(db: AsyncSession, since: datetime, until: datetime, bucket_seconds: int, offset_seconds: int) -> dict[int, tuple[int, int]]:
    """{bucket index: (rides created, of which COMPLETED)}. Bucket index = floor((epoch + offset) / bucket size)."""
    bucket = func.floor((extract("epoch", Ride.created_at) + offset_seconds) / bucket_seconds)
    result = await db.execute(
        select(bucket.label("bucket"), func.count(), func.count().filter(Ride.status == RideStatus.COMPLETED))
        .where(Ride.created_at >= since, Ride.created_at < until)
        .group_by(bucket)
    )
    return {int(index): (rides, completed) for index, rides, completed in result.all()}


async def money_series(db: AsyncSession, since: datetime, until: datetime, bucket_seconds: int, offset_seconds: int) -> dict[int, tuple[int, int]]:
    """{bucket index: (gross, platform fee)} of the earnings settled in each bucket."""
    bucket = func.floor((extract("epoch", RideEarning.created_at) + offset_seconds) / bucket_seconds)
    result = await db.execute(
        select(bucket.label("bucket"), func.sum(RideEarning.gross_amount), func.sum(RideEarning.platform_fee))
        .where(RideEarning.created_at >= since, RideEarning.created_at < until)
        .group_by(bucket)
    )
    return {int(index): (gross, platform_fee) for index, gross, platform_fee in result.all()}


async def count_pending_offers(db: AsyncSession) -> int:
    """PENDING offers right now, an expired one the sweeper has not handled yet included."""
    result = await db.execute(select(func.count()).select_from(RideOffer).where(RideOffer.status == OfferStatus.PENDING))
    return result.scalar_one()
