from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models import Driver, VerificationStatus, Vehicle

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
