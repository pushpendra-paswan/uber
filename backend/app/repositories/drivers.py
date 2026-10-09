import time

from sqlalchemy import exists, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app import database
from app.database import redis_client
from app.models import ACTIVE_RIDE_STATUSES, Driver, OfferStatus, Ride, RideOffer, VerificationStatus, Vehicle

GEO_KEY = "drivers:geo"
PRESENCE_KEY = "driver:{}:presence"
PRESENCE_TTL_SECONDS = 30

# populate_existing: a driver already in the session (just created, or whose vehicle was just added)
# is refreshed, so created_at and vehicle are loaded and DriverResponse can serialize it.
LOAD_DRIVER = (selectinload(Driver.user), selectinload(Driver.vehicle))


async def get_by_user_id(db: AsyncSession, user_id: int) -> Driver | None:
    result = await db.execute(
        select(Driver).where(Driver.user_id == user_id).options(*LOAD_DRIVER).execution_options(populate_existing=True)
    )
    return result.scalar_one_or_none()


async def get_by_id(db: AsyncSession, driver_id: int) -> Driver | None:
    result = await db.execute(
        select(Driver).where(Driver.id == driver_id).options(*LOAD_DRIVER).execution_options(populate_existing=True)
    )
    return result.scalar_one_or_none()


async def get_user_id(db: AsyncSession, driver_id: int) -> int:
    # The address of a driver's events: sockets are keyed by user id, not driver id.
    result = await db.execute(select(Driver.user_id).where(Driver.id == driver_id))
    return result.scalar_one()


async def list_all(db: AsyncSession, status: VerificationStatus | None = None) -> list[Driver]:
    query = select(Driver).options(*LOAD_DRIVER).order_by(Driver.created_at, Driver.id)
    if status is not None:
        query = query.where(Driver.verification_status == status)
    result = await db.execute(query.execution_options(populate_existing=True))
    return list(result.scalars().all())


async def create_driver(db: AsyncSession, user_id: int, license_number: str) -> Driver:
    driver = Driver(user_id=user_id, license_number=license_number, verification_status=VerificationStatus.pending)
    db.add(driver)
    await db.flush()
    return driver


async def get_vehicle_by_plate(db: AsyncSession, plate_number: str) -> Vehicle | None:
    result = await db.execute(select(Vehicle).where(Vehicle.plate_number == plate_number))
    return result.scalar_one_or_none()


async def create_vehicle(db: AsyncSession, driver_id: int, plate_number: str, model: str, color: str) -> Vehicle:
    vehicle = Vehicle(driver_id=driver_id, plate_number=plate_number, model=model, color=color, vehicle_type="economy")
    db.add(vehicle)
    await db.flush()
    return vehicle


# Redis: the presence key says who is online. The GEO set is only a spatial index and can hold stale
# members (a driver whose key expired), so anything that reads it must check the presence key.
# GEOADD takes longitude first, and GEOPOS returns [longitude, latitude].


async def set_online(driver_id: int, lat: float, lng: float) -> int:
    updated_at = int(time.time())
    await redis_client.set(PRESENCE_KEY.format(driver_id), updated_at, ex=PRESENCE_TTL_SECONDS)
    await redis_client.geoadd(GEO_KEY, (lng, lat, str(driver_id)))
    return updated_at


async def refresh_location(driver_id: int, lat: float, lng: float) -> int | None:
    updated_at = int(time.time())
    # XX: only written if the key exists, so check and write are one step and a stray ping cannot bring an offline driver back.
    was_online = await redis_client.set(PRESENCE_KEY.format(driver_id), updated_at, ex=PRESENCE_TTL_SECONDS, xx=True)
    if not was_online:
        return None
    await redis_client.geoadd(GEO_KEY, (lng, lat, str(driver_id)))
    return updated_at


async def set_offline(driver_id: int) -> None:
    await redis_client.delete(PRESENCE_KEY.format(driver_id))
    await redis_client.zrem(GEO_KEY, str(driver_id))


async def get_presence(driver_id: int) -> dict | None:
    updated_at = await redis_client.get(PRESENCE_KEY.format(driver_id))
    if updated_at is None:
        return None
    position = (await redis_client.geopos(GEO_KEY, str(driver_id)))[0]
    if position is None:
        return None
    lng, lat = position
    return {"lat": lat, "lng": lng, "updated_at": int(updated_at)}


async def search_nearby(lat: float, lng: float, radius_m: int, limit: int) -> list[tuple[int, float]]:
    # Nearest first. The GEO set can hold stale members, so the caller still has to check the presence key.
    found = await redis_client.geosearch(
        GEO_KEY, longitude=lng, latitude=lat, radius=radius_m, unit="m", sort="ASC", count=limit, withdist=True
    )
    return [(int(driver_id), distance_m) for driver_id, distance_m in found]


async def get_online_ids(driver_ids: list[int]) -> set[int]:
    values = await redis_client.mget([PRESENCE_KEY.format(driver_id) for driver_id in driver_ids])
    return {driver_id for driver_id, value in zip(driver_ids, values) if value is not None}


async def remove_from_geo(driver_ids: list[int]) -> None:
    await redis_client.zrem(GEO_KEY, *[str(driver_id) for driver_id in driver_ids])


async def get_available_ids(db: AsyncSession, driver_ids: list[int]) -> set[int]:
    if not driver_ids:
        return set()
    has_active_ride = exists().where(Ride.driver_id == Driver.id, Ride.status.in_(ACTIVE_RIDE_STATUSES))
    # A driver deciding on an offer is busy.
    has_pending_offer = exists().where(RideOffer.driver_id == Driver.id, RideOffer.status == OfferStatus.PENDING)
    result = await db.execute(
        select(Driver.id).where(
            Driver.id.in_(driver_ids),
            Driver.verification_status == VerificationStatus.approved,
            ~has_active_ride,
            ~has_pending_offer,
        )
    )
    return set(result.scalars().all())


async def try_lock(db: AsyncSession, driver_id: int) -> bool:
    """Takes the driver's row (FOR UPDATE SKIP LOCKED) until commit or rollback. False means someone else holds it."""
    result = await db.execute(select(Driver.id).where(Driver.id == driver_id).with_for_update(skip_locked=True))
    return result.first() is not None


async def lock(db: AsyncSession, driver_id: int) -> None:
    """Blocks until this driver's row is ours (FOR UPDATE), at most LOCK_WAIT_MS; the lock lasts until commit or rollback."""
    await db.execute(text(f"SET LOCAL lock_timeout = {int(database.LOCK_WAIT_MS)}"))
    await db.execute(select(Driver.id).where(Driver.id == driver_id).with_for_update())
