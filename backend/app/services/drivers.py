from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Driver, User, VerificationStatus
from app.repositories import drivers as drivers_repo
from app.schemas import DriverProfileCreate, VehicleCreate


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
    return driver
