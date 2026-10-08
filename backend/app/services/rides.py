from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import Ride, RideEvent, RideStatus, User, UserRole
from app.repositories import drivers as drivers_repo
from app.repositories import rides as rides_repo
from app.schemas import EstimateRequest, RideCreate
from app.services import matching, pricing, routing
from app.utils.geo import is_inside_bounds

MIN_TRIP_DISTANCE_M = 200

ALLOWED_TRANSITIONS = {
    RideStatus.REQUESTED: {RideStatus.DRIVER_ASSIGNED, RideStatus.CANCELLED, RideStatus.NO_DRIVER_FOUND},
    RideStatus.DRIVER_ASSIGNED: {RideStatus.DRIVER_ARRIVED, RideStatus.CANCELLED},
    RideStatus.DRIVER_ARRIVED: {RideStatus.IN_PROGRESS, RideStatus.CANCELLED},
    RideStatus.IN_PROGRESS: {RideStatus.COMPLETED},
    RideStatus.COMPLETED: set(),
    RideStatus.CANCELLED: set(),
    RideStatus.NO_DRIVER_FOUND: set(),
}


async def change_ride_status(db: AsyncSession, ride: Ride, new_status: RideStatus, actor_user_id: int | None) -> None:
    """The only place that changes ride.status after creation. Does not commit."""
    old_status = ride.status
    if new_status not in ALLOWED_TRANSITIONS[old_status]:
        raise HTTPException(status_code=409, detail=f"Cannot change ride from {old_status.value} to {new_status.value}")

    ride.status = new_status
    if new_status == RideStatus.IN_PROGRESS:
        ride.started_at = datetime.now(timezone.utc)
    if new_status == RideStatus.COMPLETED:
        ride.completed_at = datetime.now(timezone.utc)
    await rides_repo.add_event(db, ride.id, old_status, new_status, actor_user_id)


async def estimate_ride(db: AsyncSession, data: EstimateRequest) -> dict:
    bounds = (settings.city_south, settings.city_west, settings.city_north, settings.city_east)
    if not is_inside_bounds(data.pickup_lat, data.pickup_lng, *bounds):
        raise HTTPException(status_code=422, detail="Pickup is outside the service area")
    if not is_inside_bounds(data.dropoff_lat, data.dropoff_lng, *bounds):
        raise HTTPException(status_code=422, detail="Drop-off is outside the service area")

    route = await routing.get_route(data.pickup_lat, data.pickup_lng, data.dropoff_lat, data.dropoff_lng)
    if route["distance_m"] < MIN_TRIP_DISTANCE_M:
        raise HTTPException(status_code=422, detail="Pickup and drop-off are too close for a ride")

    fare = await pricing.calculate_fare(db, route["distance_m"], route["duration_s"])
    return {**route, **fare}


async def create_ride(db: AsyncSession, rider: User, data: RideCreate) -> Ride:
    # Checked first, so a rider who already has a ride never costs a routing call.
    if await rides_repo.get_active_for_rider(db, rider.id) is not None:
        raise HTTPException(status_code=409, detail="You already have an active ride")

    # The client never sends distance, time, or fare: the server always works them out itself.
    estimate = await estimate_ride(db, data)

    # Not routed through change_ride_status: there is no previous status.
    ride = await rides_repo.create(db, rider.id, data, estimate["distance_m"], estimate["duration_s"], estimate["fare_estimate"])
    await rides_repo.add_event(db, ride.id, None, RideStatus.REQUESTED, rider.id)

    # Matching runs in the same transaction, so a Redis or OSRM failure leaves no half-created ride.
    # The system makes these changes, so the actor is null.
    driver_id = await matching.find_driver(db, ride.pickup_lat, ride.pickup_lng)
    if driver_id is None:
        await change_ride_status(db, ride, RideStatus.NO_DRIVER_FOUND, actor_user_id=None)
    else:
        await change_ride_status(db, ride, RideStatus.DRIVER_ASSIGNED, actor_user_id=None)
        ride.driver_id = driver_id
    await db.commit()
    return ride


async def load_ride_for_user(db: AsyncSession, user: User, ride_id: int, for_update: bool = False) -> Ride:
    # A ride you are not part of looks the same as a ride that does not exist.
    not_found = HTTPException(status_code=404, detail="Ride not found")
    ride = await rides_repo.get_by_id(db, ride_id, for_update)
    if ride is None:
        raise not_found

    if user.role == UserRole.rider and ride.rider_id != user.id:
        raise not_found
    if user.role == UserRole.driver:
        driver = await drivers_repo.get_by_user_id(db, user.id)
        if driver is None or ride.driver_id != driver.id:
            raise not_found
    return ride


async def get_ride(db: AsyncSession, user: User, ride_id: int) -> Ride:
    return await load_ride_for_user(db, user, ride_id)


async def get_events(db: AsyncSession, user: User, ride_id: int) -> list[RideEvent]:
    await load_ride_for_user(db, user, ride_id)
    return await rides_repo.list_events(db, ride_id)


async def get_active(db: AsyncSession, user: User) -> Ride:
    if user.role == UserRole.rider:
        ride = await rides_repo.get_active_for_rider(db, user.id)
    else:
        driver = await drivers_repo.get_by_user_id(db, user.id)
        ride = await rides_repo.get_active_for_driver(db, driver.id) if driver is not None else None
    if ride is None:
        raise HTTPException(status_code=404, detail="No active ride")
    return ride


async def driver_set_status(db: AsyncSession, user: User, ride_id: int, new_status: RideStatus) -> Ride:
    ride = await load_ride_for_user(db, user, ride_id, for_update=True)
    await change_ride_status(db, ride, new_status, user.id)
    await db.commit()
    return ride


async def cancel(db: AsyncSession, user: User, ride_id: int) -> Ride:
    ride = await load_ride_for_user(db, user, ride_id, for_update=True)
    await change_ride_status(db, ride, RideStatus.CANCELLED, user.id)
    await db.commit()
    return ride
