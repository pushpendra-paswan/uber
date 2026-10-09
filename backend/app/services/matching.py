from datetime import datetime, timedelta, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Ride, RideOffer
from app.repositories import drivers as drivers_repo
from app.repositories import offers as offers_repo

SEARCH_RADIUS_M = 3000
MAX_CANDIDATES = 50
OFFER_TIMEOUT_SECONDS = 15
MAX_OFFERS_PER_RIDE = 5


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

    available_ids = await drivers_repo.get_available_ids(db, list(online_ids))
    candidates = [
        (distance_m, driver_id) for driver_id, distance_m in nearby if driver_id in available_ids and driver_id not in previous
    ]
    if not candidates:
        return None

    # Ties on distance go to the lower driver id, so the result is the same every time.
    distance_m, driver_id = min(candidates)
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=OFFER_TIMEOUT_SECONDS)
    return await offers_repo.create(db, ride.id, driver_id, round(distance_m), expires_at)
