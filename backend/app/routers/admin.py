from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import Driver, VerificationStatus
from app.schemas import DriverResponse
from app.security import require_role
from app.services import drivers as drivers_service

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
