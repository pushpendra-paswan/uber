from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ACTIVE_RIDE_STATUSES, RideStatus, VerificationStatus
from app.repositories import admin as admin_repo
from app.repositories import drivers as drivers_repo
from app.repositories import rides as rides_repo
from app.utils.ratings import average

LIVE_MAX_DRIVERS = 1000
LIVE_MAX_RIDES = 1000
DRIVERS_DEFAULT_LIMIT = 50
DRIVERS_MAX_LIMIT = 200
RIDES_DEFAULT_LIMIT = 25
RIDES_MAX_LIMIT = 100


async def get_live(db: AsyncSession) -> dict:
    """The online drivers on the map and the active rides.

    This is NOT one atomic snapshot: it is Redis (positions and presence) followed by several plain Postgres reads, none of
    them locked, so a driver can accept a ride between two statements. Anything that must agree inside this answer comes
    from the same rows: the driver counts from the returned drivers, the ride counts from the one grouped query.
    A Redis failure propagates (503): a map without positions is meaningless."""
    positions = await drivers_repo.get_online_positions()
    online_ids = sorted(positions)
    kept_ids = online_ids[:LIVE_MAX_DRIVERS]
    names = await admin_repo.list_live_drivers(db, kept_ids)
    states = await admin_repo.get_driver_states(db, kept_ids)

    drivers = []
    counts = {"online": 0, "free": 0, "offered": 0, "on_ride": 0}
    for row in names:
        driver_state = states[row["id"]]
        # On a ride beats offered beats free (every driver here is online: they come from the presence keys).
        if driver_state["active_ride_id"] is not None:
            state = "on_ride"
        elif driver_state["has_pending_offer"]:
            state = "offered"
        else:
            state = "free"
        lat, lng = positions[row["id"]]
        drivers.append(
            {"id": row["id"], "name": row["name"], "plate_number": row["plate_number"], "lat": lat, "lng": lng, "state": state,
             "active_ride_id": driver_state["active_ride_id"]}
        )
        counts["online"] += 1
        counts[state] += 1

    ride_rows = await admin_repo.list_active_rides(db, LIVE_MAX_RIDES)
    ride_counts = await admin_repo.count_active_rides_by_status(db)
    return {
        "generated_at": int(datetime.now(timezone.utc).timestamp()),
        "drivers": drivers,
        "rides": ride_rows[:LIVE_MAX_RIDES],
        "drivers_total": len(online_ids),
        "rides_total": sum(ride_counts.values()),
        "truncated": len(online_ids) > LIVE_MAX_DRIVERS or len(ride_rows) > LIVE_MAX_RIDES,
        "counts": {"drivers": counts, "rides": {status.value: ride_counts.get(status.value, 0) for status in ACTIVE_RIDE_STATUSES}},
    }


async def list_drivers(db: AsyncSession, status: VerificationStatus | None, q: str | None, limit: int, after_id: int | None) -> list[dict]:
    if not 1 <= limit <= DRIVERS_MAX_LIMIT:
        raise HTTPException(status_code=422, detail=f"limit must be between 1 and {DRIVERS_MAX_LIMIT}")
    if q is not None:
        q = q.strip()
        if not 1 <= len(q) <= 100:
            raise HTTPException(status_code=422, detail="q must be between 1 and 100 characters")

    rows = await admin_repo.list_drivers(db, status, q, limit, after_id)
    ids = [row["driver"].id for row in rows]
    online_ids = await drivers_repo.get_online_ids(ids) if ids else set()
    states = await admin_repo.get_driver_states(db, ids)

    drivers = []
    for row in rows:
        driver = row["driver"]
        driver_state = states[driver.id]
        if driver_state["active_ride_id"] is not None:
            state = "on_ride"  # even when the presence key expired: the ride is still theirs
        elif driver.id not in online_ids:
            state = "offline"
        elif driver_state["has_pending_offer"]:
            state = "offered"
        else:
            state = "free"
        drivers.append(
            {
                "id": driver.id, "license_number": driver.license_number, "verification_status": driver.verification_status,
                "created_at": driver.created_at, "user": driver.user, "vehicle": driver.vehicle, "user_id": driver.user_id,
                "online": driver.id in online_ids, "state": state, "active_ride_id": driver_state["active_ride_id"],
                "rating_count": row["rating_count"], "rating_average": average(row["rating_count"], row["rating_total"]),
                "completed_trips": row["completed_trips"],
            }
        )
    return drivers


async def list_rides(
    db: AsyncSession, status: RideStatus | None, rider_id: int | None, driver_id: int | None, since: datetime | None,
    until: datetime | None, limit: int, before_id: int | None,
) -> list[dict]:
    if not 1 <= limit <= RIDES_MAX_LIMIT:
        raise HTTPException(status_code=422, detail=f"limit must be between 1 and {RIDES_MAX_LIMIT}")
    if since is not None and until is not None and until <= since:
        raise HTTPException(status_code=422, detail="until must be after since")
    return await admin_repo.list_rides(db, status, rider_id, driver_id, since, until, limit, before_id)


async def get_ride(db: AsyncSession, ride_id: int) -> dict:
    ride = await admin_repo.get_ride_detail(db, ride_id)
    if ride is None:
        raise HTTPException(status_code=404, detail="Ride not found")

    events = []
    for event in await rides_repo.list_events(db, ride_id):
        if event.actor_user_id is None:
            actor = "system"
        elif event.actor_user_id == ride["rider_id"]:
            actor = "rider"
        elif event.actor_user_id == ride["driver_user_id"]:
            actor = "driver"
        else:
            actor = "other"
        events.append(
            {"id": event.id, "from_status": event.from_status, "to_status": event.to_status, "actor_user_id": event.actor_user_id,
             "actor": actor, "created_at": event.created_at}
        )

    driver = None
    if ride["driver_id"] is not None:
        driver = {
            "id": ride["driver_id"], "user_id": ride["driver_user_id"], "name": ride["driver_name"], "email": ride["driver_email"],
            "plate_number": ride["plate_number"], "model": ride["vehicle_model"], "color": ride["vehicle_color"],
        }
    ratings = [
        {**rating, "from_role": "rider" if rating["from_user_id"] == ride["rider_id"] else "driver"}
        for rating in await admin_repo.list_ride_ratings(db, ride_id)
    ]
    return {
        **ride,
        "rider": {"id": ride["rider_id"], "name": ride["rider_name"], "email": ride["rider_email"]},
        "driver": driver,
        "events": events,
        "offers": await admin_repo.list_ride_offers(db, ride_id),
        "payment": await admin_repo.get_ride_payment(db, ride_id),
        "earning": await admin_repo.get_ride_earning(db, ride_id),
        "ratings": ratings,
    }
