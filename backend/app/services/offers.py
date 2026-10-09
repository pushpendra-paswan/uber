import asyncio
import logging
from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app import database
from app.models import OfferStatus, Ride, RideOffer, RideStatus, User, VerificationStatus
from app.repositories import drivers as drivers_repo
from app.repositories import events
from app.repositories import offers as offers_repo
from app.repositories import rides as rides_repo
from app.schemas import OfferResponse
from app.services import matching
from app.services.drivers import get_me
from app.services.rides import change_ride_status

OFFER_SWEEP_INTERVAL_SECONDS = 1
DUE_BATCH_SIZE = 50

# Lock order everywhere: the ride row first, then the offer row. Accept, reject, expiry, and cancel all follow it,
# so two of them can wait for each other's ride lock but never for each other's offer lock (no deadlock).

# uvicorn's logger, because it is the one that has a handler and prints INFO.
logger = logging.getLogger("uvicorn.error")


async def get_pending(db: AsyncSession, user: User) -> OfferResponse:
    driver = await get_me(db, user)
    now = datetime.now(timezone.utc)
    offer = await offers_repo.get_oldest_pending_for_driver(db, driver.id, now)
    if offer is None:
        raise HTTPException(status_code=404, detail="No pending offer")

    ride = await rides_repo.get_by_id(db, offer.ride_id)
    return OfferResponse(
        id=offer.id,
        ride_id=ride.id,
        pickup_address=ride.pickup_address,
        pickup_lat=ride.pickup_lat,
        pickup_lng=ride.pickup_lng,
        dropoff_address=ride.dropoff_address,
        dropoff_lat=ride.dropoff_lat,
        dropoff_lng=ride.dropoff_lng,
        trip_distance_m=ride.distance_m,
        trip_duration_s=ride.duration_s,
        fare_estimate=ride.fare_estimate,
        pickup_distance_m=offer.pickup_distance_m,
        expires_in=round(max(0.0, (offer.expires_at - now).total_seconds()), 1),
    )


async def accept(db: AsyncSession, user: User, offer_id: int) -> Ride:
    driver = await get_me(db, user)
    ids = await offers_repo.get_ride_and_driver_ids(db, offer_id)
    # Someone else's offer looks the same as an offer that does not exist.
    if ids is None or ids.driver_id != driver.id:
        raise HTTPException(status_code=404, detail="Offer not found")

    ride = await rides_repo.get_by_id(db, ids.ride_id, for_update=True)
    offer = await offers_repo.get_by_id(db, offer_id, for_update=True)

    now = datetime.now(timezone.utc)
    if offer.status != OfferStatus.PENDING:
        raise HTTPException(status_code=409, detail="This offer is no longer available")
    # The deadline is hard even if the sweeper has not run yet.
    if offer.expires_at <= now:
        raise HTTPException(status_code=409, detail="This offer has expired")
    if driver.verification_status != VerificationStatus.approved:
        raise HTTPException(
            status_code=403, detail=f"Your driver account is not approved (status: {driver.verification_status.value})"
        )
    if await rides_repo.get_active_for_driver(db, driver.id) is not None:
        raise HTTPException(status_code=409, detail="You already have an active ride")
    if await drivers_repo.get_presence(driver.id) is None:
        raise HTTPException(status_code=409, detail="You are offline. Go online first.")

    await offers_repo.set_status(db, offer, OfferStatus.ACCEPTED, now)
    await change_ride_status(db, ride, RideStatus.DRIVER_ASSIGNED, user.id)
    ride.driver_id = driver.id
    await db.commit()

    logger.info("Offer %s accepted: ride %s assigned to driver %s", offer.id, ride.id, driver.id)
    await events.publish(ride.rider_id, "ride_updated", {"ride_id": ride.id, "status": ride.status.value})
    await events.publish(user.id, "offer_closed", {"offer_id": offer.id, "ride_id": ride.id, "reason": "accepted"})
    return ride


async def reject(db: AsyncSession, user: User, offer_id: int) -> None:
    driver = await get_me(db, user)
    ids = await offers_repo.get_ride_and_driver_ids(db, offer_id)
    if ids is None or ids.driver_id != driver.id:
        raise HTTPException(status_code=404, detail="Offer not found")

    ride = await rides_repo.get_by_id(db, ids.ride_id, for_update=True)
    offer = await offers_repo.get_by_id(db, offer_id, for_update=True)

    if offer.status != OfferStatus.PENDING:
        raise HTTPException(status_code=409, detail="This offer is no longer available")
    if offer.expires_at <= datetime.now(timezone.utc):
        raise HTTPException(status_code=409, detail="This offer has expired")

    await finish_offer(db, ride, offer, OfferStatus.REJECTED, "rejected")


async def finish_offer(db: AsyncSession, ride: Ride, offer: RideOffer, new_status: OfferStatus, reason: str) -> None:
    """Closes an offer that was rejected or ran out of time, then offers the ride onward or ends it.

    The caller has locked the ride and the offer and checked that the offer is still PENDING.
    """
    responded_at = datetime.now(timezone.utc) if new_status == OfferStatus.REJECTED else None
    await offers_repo.set_status(db, offer, new_status, responded_at)
    driver_user_id = await drivers_repo.get_user_id(db, offer.driver_id)

    next_offer = await matching.offer_to_next_driver(db, ride)
    if next_offer is None:
        await change_ride_status(db, ride, RideStatus.NO_DRIVER_FOUND, actor_user_id=None)
    else:
        next_user_id = await drivers_repo.get_user_id(db, next_offer.driver_id)
    await db.commit()

    logger.info("Offer %s %s: ride %s, driver %s", offer.id, reason, ride.id, offer.driver_id)
    await events.publish(driver_user_id, "offer_closed", {"offer_id": offer.id, "ride_id": ride.id, "reason": reason})
    if next_offer is None:
        logger.info("Ride %s ended as NO_DRIVER_FOUND after %s", ride.id, reason)
        await events.publish(ride.rider_id, "ride_updated", {"ride_id": ride.id, "status": ride.status.value})
    else:
        logger.info(
            "Offer %s created: ride %s to driver %s (%s m away)", next_offer.id, ride.id, next_offer.driver_id, next_offer.pickup_distance_m
        )
        await events.publish(next_user_id, "offer_created", {"offer_id": next_offer.id, "ride_id": ride.id})


async def expire_due_offers() -> None:
    # Own sessions, looked up at call time (tests replace database.async_session): this runs outside any request.
    async with database.async_session() as db:
        due = await offers_repo.list_due(db, datetime.now(timezone.utc), DUE_BATCH_SIZE)

    for offer_id, ride_id in due:
        # A new session per offer: one failure rolls back only that offer, and locks are held for the shortest time.
        async with database.async_session() as db:
            ride = await rides_repo.get_by_id(db, ride_id, for_update=True)
            offer = await offers_repo.get_by_id(db, offer_id, for_update=True)
            # Answered or cancelled since the list was read, or (clock aside) not due after all: nothing to do.
            if offer.status != OfferStatus.PENDING or offer.expires_at > datetime.now(timezone.utc):
                continue
            await finish_offer(db, ride, offer, OfferStatus.EXPIRED, "expired")


async def sweep_forever() -> None:
    """Runs for the life of the process. State is in Postgres, so a restart loses nothing."""
    failing = False
    while True:
        try:
            await expire_due_offers()
            if failing:
                logger.info("Offer sweeper recovered")
                failing = False
        except Exception as error:
            # One line when a streak of failures starts, not one per second.
            if not failing:
                logger.warning("Offer sweeper failed, retrying every %s s: %r", OFFER_SWEEP_INTERVAL_SECONDS, error)
                failing = True
        await asyncio.sleep(OFFER_SWEEP_INTERVAL_SECONDS)
