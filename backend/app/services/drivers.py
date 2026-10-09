from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import Driver, RideStatus, User, VerificationStatus
from app.repositories import drivers as drivers_repo
from app.repositories import events
from app.repositories import rides as rides_repo
from app.schemas import DriverProfileCreate, LocationUpdate, PresenceResponse, VehicleCreate
from app.services import pricing
from app.utils.geo import is_inside_bounds


async def create_profile(db: AsyncSession, user: User, data: DriverProfileCreate) -> Driver:
    if await drivers_repo.get_by_user_id(db, user.id) is not None:
        raise HTTPException(status_code=409, detail="You already have a driver profile")

    # The unique constraint on user_id is the real protection against two simultaneous requests.
    try:
        driver = await drivers_repo.create_driver(db, user.id, data.license_number)
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(status_code=409, detail="You already have a driver profile")
    return await drivers_repo.get_by_id(db, driver.id)


async def add_vehicle(db: AsyncSession, user: User, data: VehicleCreate) -> Driver:
    driver = await drivers_repo.get_by_user_id(db, user.id)
    if driver is None:
        raise HTTPException(status_code=409, detail="Create your driver profile before adding a vehicle")
    if driver.vehicle is not None:
        raise HTTPException(status_code=409, detail="You already have a vehicle")

    # "ka 01-ab 1234" and "KA01AB1234" are the same plate.
    plate_number = data.plate_number.upper().replace(" ", "").replace("-", "")
    if await drivers_repo.get_vehicle_by_plate(db, plate_number) is not None:
        raise HTTPException(status_code=409, detail="This plate number is already registered")

    # The unique constraints on plate_number and driver_id are the real protection against simultaneous requests.
    try:
        await drivers_repo.create_vehicle(db, driver.id, plate_number, data.model, data.color)
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(status_code=409, detail="This plate number is taken or you already have a vehicle")
    return await drivers_repo.get_by_id(db, driver.id)


async def get_me(db: AsyncSession, user: User) -> Driver:
    driver = await drivers_repo.get_by_user_id(db, user.id)
    if driver is None:
        raise HTTPException(status_code=404, detail="You have no driver profile yet")
    return driver


async def list_for_admin(db: AsyncSession, status: VerificationStatus | None) -> list[Driver]:
    return await drivers_repo.list_all(db, status)


async def set_verification(db: AsyncSession, driver_id: int, new_status: VerificationStatus) -> Driver:
    driver = await drivers_repo.get_by_id(db, driver_id)
    if driver is None:
        raise HTTPException(status_code=404, detail="Driver not found")
    if driver.verification_status == new_status:
        raise HTTPException(status_code=409, detail=f"Driver is already {new_status.value}")
    if new_status == VerificationStatus.approved and driver.vehicle is None:
        raise HTTPException(status_code=409, detail="Cannot approve a driver who has no vehicle")

    driver.verification_status = new_status
    await db.commit()
    if new_status != VerificationStatus.approved:
        await drivers_repo.set_offline(driver.id)
    return driver


async def go_online(db: AsyncSession, user: User, data: LocationUpdate) -> PresenceResponse:
    driver = await get_me(db, user)
    if driver.verification_status != VerificationStatus.approved:
        raise HTTPException(
            status_code=403, detail=f"Your driver account is not approved (status: {driver.verification_status.value})"
        )
    if not is_inside_bounds(
        data.lat, data.lng, settings.city_south, settings.city_west, settings.city_north, settings.city_east
    ):
        raise HTTPException(status_code=422, detail="Location is outside the service area")

    updated_at = await drivers_repo.set_online(driver.id, data.lat, data.lng)
    return PresenceResponse(online=True, lat=data.lat, lng=data.lng, updated_at=updated_at)


async def go_offline(db: AsyncSession, user: User) -> PresenceResponse:
    driver = await get_me(db, user)
    if await rides_repo.get_active_for_driver(db, driver.id) is not None:
        raise HTTPException(status_code=409, detail="Finish or cancel your current ride before going offline")

    await drivers_repo.set_offline(driver.id)
    return PresenceResponse(online=False)


async def update_location(db: AsyncSession, user: User, data: LocationUpdate) -> PresenceResponse:
    driver = await get_me(db, user)
    if driver.verification_status != VerificationStatus.approved:
        raise HTTPException(
            status_code=403, detail=f"Your driver account is not approved (status: {driver.verification_status.value})"
        )
    if not is_inside_bounds(
        data.lat, data.lng, settings.city_south, settings.city_west, settings.city_north, settings.city_east
    ):
        raise HTTPException(status_code=422, detail="Location is outside the service area")

    updated_at = await drivers_repo.refresh_location(driver.id, data.lat, data.lng)
    if updated_at is None:
        raise HTTPException(status_code=409, detail="You are offline. Go online first.")

    # An active ride always has a driver, so the rider can be told where the driver is.
    ride = await rides_repo.get_active_for_driver(db, driver.id)
    if ride is not None:
        await events.publish(
            ride.rider_id, "driver_location", {"ride_id": ride.id, "lat": data.lat, "lng": data.lng, "updated_at": updated_at}
        )
        if ride.status == RideStatus.IN_PROGRESS:
            await pricing.record_trip_point(ride.id, data.lat, data.lng)
    return PresenceResponse(online=True, lat=data.lat, lng=data.lng, updated_at=updated_at)


async def get_presence(db: AsyncSession, user: User) -> PresenceResponse:
    driver = await get_me(db, user)
    presence = await drivers_repo.get_presence(driver.id)
    if presence is None:
        return PresenceResponse(online=False)
    return PresenceResponse(online=True, **presence)
