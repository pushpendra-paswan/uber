import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Ride, RideOffer
from app.repositories import drivers as drivers_repo
from app.repositories import offers as offers_repo

SEARCH_RADIUS_M = 3000
MAX_CANDIDATES = 50
OFFER_TIMEOUT_SECONDS = 15
MAX_OFFERS_PER_RIDE = 5

# uvicorn's logger, because it is the one that has a handler and prints WARNING.
logger = logging.getLogger("uvicorn.error")


async def offer_to_next_driver(db: AsyncSession, ride: Ride) -> RideOffer | None:
    """Offers the ride to the nearest free driver who has not had it yet, or returns None.

    Never changes the ride, never commits, never publishes: the caller does that.
    """
    previous = await offers_repo.list_driver_ids_for_ride(db, ride.id)
    if len(previous) >= MAX_OFFERS_PER_RIDE:
        return None

    nearby = await drivers_repo.search_nearby(ride.pickup_lat, ride.pickup_lng, SEARCH_RADIUS_M, MAX_CANDIDATES)
    if not nearby:
        return None

    nearby_ids = [driver_id for driver_id, _ in nearby]
    online_ids = await drivers_repo.get_online_ids(nearby_ids)
    # A GEO member without a presence key is a driver who stopped pinging: drop it from the index.
    stale_ids = [driver_id for driver_id in nearby_ids if driver_id not in online_ids]
    if stale_ids:
        await drivers_repo.remove_from_geo(stale_ids)

    # Stale is fine here: this only avoids taking locks for drivers who are plainly busy.
    available_ids = await drivers_repo.get_available_ids(db, list(online_ids))
    # Ties on distance go to the lower driver id, so the result is the same every time.
    candidates = sorted(
        (distance_m, driver_id) for driver_id, distance_m in nearby if driver_id in available_ids and driver_id not in previous
    )

    # Lock, then check as a separate statement, then write. The lock is the driver's row (FOR UPDATE SKIP LOCKED) and lasts
    # until the caller commits or rolls back. A driver whose row is locked is skipped, never waited for: someone else is
    # deciding about that driver right now, and waiting would line up the whole city behind one driver.
    # Every lock taken here is non-blocking, so it cannot be part of a cycle with the blocking locks of accept and create_ride.
    for distance_m, driver_id in candidates:
        if not await drivers_repo.try_lock(db, driver_id):
            continue
        # Under READ COMMITTED a statement that starts after the lock sees everything the previous lock holder committed.
        # The prefilter above started before the lock, so it can be stale: this check decides.
        if not await drivers_repo.get_available_ids(db, [driver_id]):
            continue

        expires_at = datetime.now(timezone.utc) + timedelta(seconds=OFFER_TIMEOUT_SECONDS)
        # The unique index on pending offers per driver is the last line of defense. With the lock working it never fires,
        # so a warning here means a lock failed. The savepoint undoes only the insert: the transaction, and the locks and
        # rows of the request, stay valid and matching moves on to the next candidate.
        try:
            async with db.begin_nested():
                return await offers_repo.create(db, ride.id, driver_id, round(distance_m), expires_at)
        except IntegrityError as error:
            logger.warning("offer skipped for driver %s: %s", driver_id, error.orig)
    return None
