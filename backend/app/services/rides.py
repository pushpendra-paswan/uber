import logging
import secrets
from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import ACTIVE_RIDE_STATUSES, OfferStatus, Ride, RideEvent, RideStatus, User, UserRole
from app.repositories import drivers as drivers_repo
from app.repositories import events
from app.repositories import offers as offers_repo
from app.repositories import rides as rides_repo
from app.repositories import users as users_repo
from app.schemas import CancellationFeeResponse, DriverLocation, EstimateRequest, OtpResponse, RideCreate, RideDriverResponse, VehicleResponse
from app.services import matching, pricing, routing
from app.utils.geo import is_inside_bounds

MIN_TRIP_DISTANCE_M = 200

# Fake on purpose, so the flow can be learned and tested by hand. A real system would make four random digits
# per ride with the secrets module. It is still stored per ride, shown only to the rider, and checked on start.
FAKE_OTP = "1234"

# uvicorn's logger, because it is the one that has a handler and prints INFO.
logger = logging.getLogger("uvicorn.error")

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
    # The only place the trip code changes: set on assignment, gone once the trip starts or the ride is cancelled.
    if new_status == RideStatus.DRIVER_ASSIGNED:
        ride.otp = FAKE_OTP
    if new_status in (RideStatus.IN_PROGRESS, RideStatus.CANCELLED):
        ride.otp = None
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

    zone, surge_percent = await pricing.get_surge_percent(db, data.pickup_lat, data.pickup_lng)
    fare = await pricing.calculate_fare(db, route["distance_m"], route["duration_s"], surge_percent)
    return {**route, **fare, "zone": zone}


async def create_ride(db: AsyncSession, rider: User, data: RideCreate) -> Ride:
    # Checked first, so a rider who already has a ride never costs a routing call.
    if await rides_repo.get_active_for_rider(db, rider.id) is not None:
        raise HTTPException(status_code=409, detail="You already have an active ride")

    # The client never sends distance, time, or fare: the server always works them out itself.
    estimate = await estimate_ride(db, data)

    # The rider is never charged a higher multiplier than the one they saw. A lower one is charged as it is, and no
    # accepted value means "the current one". Checked before any lock, like every surge read.
    if data.accepted_surge_percent is not None and estimate["surge_percent"] > data.accepted_surge_percent:
        raise HTTPException(
            status_code=409,
            detail=f"Prices have increased in your area (now {estimate['surge_percent'] / 100:.1f}x). "
            "Please review the new fare and request again.",
        )

    # Lock, then check as a separate statement, then write. The lock is the rider's user row (FOR UPDATE), taken after the
    # routing call so it is held only for the insert and the commit, never across OSRM. It lasts until the commit below.
    # Blocking locks in this request: the user row, and nothing else. Matching takes only non-blocking driver locks
    # while it holds it, so no cycle can form.
    try:
        await users_repo.lock(db, rider.id)
    except DBAPIError as error:
        if error.orig.sqlstate != "55P03":  # lock_timeout
            raise
        raise HTTPException(status_code=503, detail="Busy, please retry")
    # This second check decides. It is its own statement, started after the lock, so under READ COMMITTED it sees a ride
    # that another request committed while we waited. The first check above is only the cheap early exit.
    if await rides_repo.get_active_for_rider(db, rider.id) is not None:
        raise HTTPException(status_code=409, detail="You already have an active ride")

    # The unique indexes on active rides are the last line of defense behind the lock: if one fires, the whole request is undone.
    try:
        # Not routed through change_ride_status: there is no previous status.
        ride = await rides_repo.create(
            db, rider.id, data, estimate["distance_m"], estimate["duration_s"], estimate["fare_estimate"],
            estimate["zone"], estimate["surge_percent"],
        )
        await rides_repo.add_event(db, ride.id, None, RideStatus.REQUESTED, rider.id)

        # The first offer is made in the same transaction, so a Redis or OSRM failure leaves no half-created ride.
        # The system makes these changes, so the actor is null. The ride stays REQUESTED while offers are being tried.
        offer = await matching.offer_to_next_driver(db, ride)
        if offer is None:
            await change_ride_status(db, ride, RideStatus.NO_DRIVER_FOUND, actor_user_id=None)
        else:
            driver_user_id = await drivers_repo.get_user_id(db, offer.driver_id)
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(status_code=409, detail="You already have an active ride")

    if offer is not None:
        logger.info("Offer %s created: ride %s to driver %s (%s m away)", offer.id, ride.id, offer.driver_id, offer.pickup_distance_m)
        await events.publish(driver_user_id, "offer_created", {"offer_id": offer.id, "ride_id": ride.id})
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


async def get_ride_driver(db: AsyncSession, user: User, ride_id: int) -> RideDriverResponse:
    ride = await load_ride_for_user(db, user, ride_id)
    if ride.driver_id is None:
        raise HTTPException(status_code=404, detail="No driver assigned to this ride")

    driver = await drivers_repo.get_by_id(db, ride.driver_id)
    # A finished ride never shows where the driver is now, even if the driver is online for another ride.
    presence = await drivers_repo.get_presence(driver.id) if ride.status in ACTIVE_RIDE_STATUSES else None
    return RideDriverResponse(
        driver_id=driver.id,
        name=driver.user.name,
        vehicle=VehicleResponse.model_validate(driver.vehicle) if driver.vehicle is not None else None,
        location=DriverLocation(**presence) if presence is not None else None,
    )


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


async def get_otp(db: AsyncSession, user: User, ride_id: int) -> OtpResponse:
    ride = await load_ride_for_user(db, user, ride_id)
    # Only the ride's own rider may see the code.
    if ride.rider_id != user.id:
        raise HTTPException(status_code=404, detail="Ride not found")
    if ride.status not in (RideStatus.DRIVER_ASSIGNED, RideStatus.DRIVER_ARRIVED) or ride.otp is None:
        raise HTTPException(status_code=409, detail="Trip code is not available")
    return OtpResponse(otp=ride.otp)


async def notify_ride_updated(db: AsyncSession, ride: Ride) -> None:
    """Tells both people on the ride that its status changed. Call it after the commit."""
    data = {"ride_id": ride.id, "status": ride.status.value}
    await events.publish(ride.rider_id, "ride_updated", data)
    if ride.driver_id is not None:
        driver_user_id = await drivers_repo.get_user_id(db, ride.driver_id)
        await events.publish(driver_user_id, "ride_updated", data)


async def driver_set_status(
    db: AsyncSession, user: User, ride_id: int, new_status: RideStatus, otp: str | None = None
) -> Ride:
    ride = await load_ride_for_user(db, user, ride_id, for_update=True)
    # The state is checked first: a wrong code on a ride that cannot start gets the 409 from change_ride_status.
    if new_status == RideStatus.IN_PROGRESS and ride.status == RideStatus.DRIVER_ARRIVED:
        if ride.otp is None or not secrets.compare_digest(ride.otp, otp):
            raise HTTPException(status_code=400, detail="Incorrect trip code")
    await change_ride_status(db, ride, new_status, user.id)
    # Settled in the same transaction, under the same ride lock, so a completed ride always has its fare.
    if new_status == RideStatus.COMPLETED:
        await pricing.settle_completed_ride(db, ride)
    await db.commit()

    await notify_ride_updated(db, ride)
    return ride


async def cancel(db: AsyncSession, user: User, ride_id: int) -> Ride:
    ride = await load_ride_for_user(db, user, ride_id, for_update=True)
    previous_status = ride.status
    await change_ride_status(db, ride, RideStatus.CANCELLED, user.id)
    await pricing.settle_cancelled_ride(db, ride, previous_status, user.role)
    # The ride is locked, and every offer change locks the ride first, so nobody can answer this offer meanwhile.
    offer = await offers_repo.get_pending_for_ride(db, ride.id)
    if offer is not None:
        await offers_repo.set_status(db, offer, OfferStatus.CANCELLED)
        driver_user_id = await drivers_repo.get_user_id(db, offer.driver_id)
    await db.commit()

    if offer is not None:
        logger.info("Offer %s cancelled: ride %s was cancelled", offer.id, ride.id)
        await events.publish(
            driver_user_id, "offer_closed", {"offer_id": offer.id, "ride_id": ride.id, "reason": "ride_cancelled"}
        )
    await notify_ride_updated(db, ride)
    return ride


async def get_cancellation_fee(db: AsyncSession, user: User, ride_id: int) -> CancellationFeeResponse:
    """What cancelling would cost the rider right now. Same function as the real cancel; no lock, nothing written."""
    ride = await load_ride_for_user(db, user, ride_id)
    if ride.status not in (RideStatus.REQUESTED, RideStatus.DRIVER_ASSIGNED, RideStatus.DRIVER_ARRIVED):
        raise HTTPException(status_code=409, detail="This ride can no longer be cancelled")
    fee, reason = await pricing.cancellation_fee(db, ride, ride.status, UserRole.rider)
    return CancellationFeeResponse(fee=fee, reason=reason)
