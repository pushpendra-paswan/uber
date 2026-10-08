from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ACTIVE_RIDE_STATUSES, Ride, RideEvent, RideStatus
from app.schemas import RideCreate


async def create(db: AsyncSession, rider_id: int, data: RideCreate) -> Ride:
    ride = Ride(rider_id=rider_id, status=RideStatus.REQUESTED, **data.model_dump())
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


async def add_event(
    db: AsyncSession, ride_id: int, from_status: RideStatus | None, to_status: RideStatus, actor_user_id: int
) -> RideEvent:
    event = RideEvent(ride_id=ride_id, from_status=from_status, to_status=to_status, actor_user_id=actor_user_id)
    db.add(event)
    await db.flush()
    return event


async def list_events(db: AsyncSession, ride_id: int) -> list[RideEvent]:
    result = await db.execute(select(RideEvent).where(RideEvent.ride_id == ride_id).order_by(RideEvent.id))
    return list(result.scalars().all())
