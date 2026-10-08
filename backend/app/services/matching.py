from sqlalchemy.ext.asyncio import AsyncSession

from app.repositories import drivers as drivers_repo

SEARCH_RADIUS_M = 3000
MAX_CANDIDATES = 50


async def find_driver(db: AsyncSession, lat: float, lng: float) -> int | None:
    """Nearest available driver to the pickup, or None. Only reads: it never changes a ride."""
    nearby = await drivers_repo.search_nearby(lat, lng, SEARCH_RADIUS_M, MAX_CANDIDATES)
    if not nearby:
        return None

    nearby_ids = [driver_id for driver_id, _ in nearby]
    online_ids = await drivers_repo.get_online_ids(nearby_ids)
    # A GEO member without a presence key is a driver who stopped pinging: drop it from the index.
    stale_ids = [driver_id for driver_id in nearby_ids if driver_id not in online_ids]
    if stale_ids:
        await drivers_repo.remove_from_geo(stale_ids)

    available_ids = await drivers_repo.get_available_ids(db, list(online_ids))
    candidates = [(distance_m, driver_id) for driver_id, distance_m in nearby if driver_id in available_ids]
    if not candidates:
        return None
    # Ties on distance go to the lower driver id, so the result is the same every time.
    return min(candidates)[1]
