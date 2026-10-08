from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import Driver, Ride, User, VerificationStatus
from app.schemas import AssignDriverRequest, DriverResponse, RideResponse
from app.security import require_role
from app.services import drivers as drivers_service
from app.services import rides as rides_service

router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(require_role("admin"))])


@router.get("/drivers", response_model=list[DriverResponse])
async def list_drivers(status: VerificationStatus | None = None, db: AsyncSession = Depends(get_db)) -> list[Driver]:
    return await drivers_service.list_for_admin(db, status)


@router.post("/drivers/{driver_id}/approve", response_model=DriverResponse)
async def approve_driver(driver_id: int, db: AsyncSession = Depends(get_db)) -> Driver:
    return await drivers_service.set_verification(db, driver_id, VerificationStatus.approved)


@router.post("/drivers/{driver_id}/reject", response_model=DriverResponse)
async def reject_driver(driver_id: int, db: AsyncSession = Depends(get_db)) -> Driver:
    return await drivers_service.set_verification(db, driver_id, VerificationStatus.rejected)


# Temporary manual stand-in for matching (M2.4), so a full ride can be clicked through before matching exists.
@router.post("/rides/{ride_id}/assign", response_model=RideResponse)
async def assign_driver(
    ride_id: int,
    data: AssignDriverRequest,
    admin: User = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
) -> Ride:
    return await rides_service.assign_driver(db, admin, ride_id, data.driver_id)
