from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import Driver, User
from app.schemas import DriverProfileCreate, DriverResponse, VehicleCreate
from app.security import require_role
from app.services import drivers as drivers_service

router = APIRouter(prefix="/drivers", tags=["drivers"])


@router.post("/me/profile", response_model=DriverResponse, status_code=201)
async def create_profile(
    data: DriverProfileCreate, user: User = Depends(require_role("driver")), db: AsyncSession = Depends(get_db)
) -> Driver:
    return await drivers_service.create_profile(db, user, data)


@router.post("/me/vehicle", response_model=DriverResponse, status_code=201)
async def add_vehicle(
    data: VehicleCreate, user: User = Depends(require_role("driver")), db: AsyncSession = Depends(get_db)
) -> Driver:
    return await drivers_service.add_vehicle(db, user, data)


@router.get("/me", response_model=DriverResponse)
async def get_me(user: User = Depends(require_role("driver")), db: AsyncSession = Depends(get_db)) -> Driver:
    return await drivers_service.get_me(db, user)
