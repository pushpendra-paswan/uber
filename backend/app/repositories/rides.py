from datetime import datetime

from sqlalchemy import distinct, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import redis_client
from app.models import ACTIVE_RIDE_STATUSES, PaymentMethod, Ride, RideEvent, RideStatus
from app.schemas import RideCreate

TRIP_KEY = "ride:{}:trip"


async def create(
    db: AsyncSession, rider_id: int, data: RideCreate, distance_m: int, duration_s: int, fare_estimate: int,
    pickup_zone: str, surge_percent: int,
) -> Ride:
    ride = Ride(
        rider_id=rider_id,
        status=RideStatus.REQUESTED,
        distance_m=distance_m,
        duration_s=duration_s,
        fare_estimate=fare_estimate,
        pickup_zone=pickup_zone,
        surge_percent=surge_percent,
        payment_method=PaymentMethod(data.payment_method),
        **data.model_dump(exclude={"accepted_surge_percent", "payment_method"}),
    )
    db.add(ride)
    await db.flush()
    return ride


async def get_by_id(db: AsyncSession, ride_id: int, for_update: bool = False) -> Ride | None:
    query = select(Ride).where(Ride.id == ride_id)
    if for_update:
        query = query.with_for_update()
    result = await db.execute(query)
    return result.scalar_one_or_none()


async def get_active_for_rider(db: AsyncSession, rider_id: int) -> Ride | None:
    result = await db.execute(select(Ride).where(Ride.rider_id == rider_id, Ride.status.in_(ACTIVE_RIDE_STATUSES)))
    return result.scalars().first()


async def get_active_for_driver(db: AsyncSession, driver_id: int) -> Ride | None:
    result = await db.execute(select(Ride).where(Ride.driver_id == driver_id, Ride.status.in_(ACTIVE_RIDE_STATUSES)))
    return result.scalars().first()


async def count_unmet_demand_by_zone(db: AsyncSession, since: datetime) -> dict[str, int]:
    """{zone: number of DISTINCT riders} with a REQUESTED or NO_DRIVER_FOUND ride created since `since`."""
    result = await db.execute(
        select(Ride.pickup_zone, func.count(distinct(Ride.rider_id)))
        .where(
            Ride.created_at >= since,
            Ride.status.in_((RideStatus.REQUESTED, RideStatus.NO_DRIVER_FOUND)),
            Ride.pickup_zone.is_not(None),
        )
        .group_by(Ride.pickup_zone)
    )
    return {zone: count for zone, count in result.all()}


async def add_event(
    db: AsyncSession, ride_id: int, from_status: RideStatus | None, to_status: RideStatus, actor_user_id: int | None
) -> RideEvent:
    event = RideEvent(ride_id=ride_id, from_status=from_status, to_status=to_status, actor_user_id=actor_user_id)
    db.add(event)
    await db.flush()
    return event


async def list_events(db: AsyncSession, ride_id: int) -> list[RideEvent]:
    result = await db.execute(select(RideEvent).where(RideEvent.ride_id == ride_id).order_by(RideEvent.id))
    return list(result.scalars().all())


async def get_last_event_time(db: AsyncSession, ride_id: int, to_status: RideStatus) -> datetime | None:
    result = await db.execute(
        select(func.max(RideEvent.created_at)).where(RideEvent.ride_id == ride_id, RideEvent.to_status == to_status)
    )
    return result.scalar_one()


async def get_trip(ride_id: int) -> dict | None:
    """The distance meter of an IN_PROGRESS ride (see pricing.record_trip_point), or None when nothing was recorded."""
    fields = await redis_client.hgetall(TRIP_KEY.format(ride_id))
    if not fields:
        return None
    return {
        "distance_m": float(fields["distance_m"]),
        "lat": float(fields["lat"]),
        "lng": float(fields["lng"]),
        "ts": float(fields["ts"]),
        "pings": int(fields["pings"]),
        "jumps": int(fields["jumps"]),
    }


async def save_trip(ride_id: int, trip: dict, ttl_seconds: int) -> None:
    key = TRIP_KEY.format(ride_id)
    async with redis_client.pipeline(transaction=True) as pipeline:
        pipeline.hset(key, mapping=trip)
        pipeline.expire(key, ttl_seconds)
        await pipeline.execute()
