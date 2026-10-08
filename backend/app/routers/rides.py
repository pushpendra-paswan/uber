from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import Ride, RideEvent, RideStatus, User
from app.schemas import EstimateRequest, EstimateResponse, RideCreate, RideDriverResponse, RideEventResponse, RideResponse
from app.security import get_current_user, require_role
from app.services import rides as rides_service

router = APIRouter(prefix="/rides", tags=["rides"])


@router.post("", response_model=RideResponse, status_code=201)
async def create_ride(
    data: RideCreate, user: User = Depends(require_role("rider")), db: AsyncSession = Depends(get_db)
) -> Ride:
    return await rides_service.create_ride(db, user, data)


# Declared before /{ride_id}, otherwise "estimate" and "active" would be read as a ride id.
@router.post("/estimate", response_model=EstimateResponse)
async def estimate_ride(
    data: EstimateRequest, user: User = Depends(require_role("rider")), db: AsyncSession = Depends(get_db)
) -> dict:
    return await rides_service.estimate_ride(db, data)


@router.get("/active", response_model=RideResponse)
async def get_active_ride(user: User = Depends(require_role("rider", "driver")), db: AsyncSession = Depends(get_db)) -> Ride:
    return await rides_service.get_active(db, user)


@router.get("/{ride_id}", response_model=RideResponse)
async def get_ride(ride_id: int, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> Ride:
    return await rides_service.get_ride(db, user, ride_id)


@router.get("/{ride_id}/driver", response_model=RideDriverResponse)
async def get_ride_driver(
    ride_id: int, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
) -> RideDriverResponse:
    return await rides_service.get_ride_driver(db, user, ride_id)


@router.get("/{ride_id}/events", response_model=list[RideEventResponse])
async def get_ride_events(
    ride_id: int, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
) -> list[RideEvent]:
    return await rides_service.get_events(db, user, ride_id)


@router.post("/{ride_id}/arrive", response_model=RideResponse)
async def arrive(ride_id: int, user: User = Depends(require_role("driver")), db: AsyncSession = Depends(get_db)) -> Ride:
    return await rides_service.driver_set_status(db, user, ride_id, RideStatus.DRIVER_ARRIVED)


@router.post("/{ride_id}/start", response_model=RideResponse)
async def start(ride_id: int, user: User = Depends(require_role("driver")), db: AsyncSession = Depends(get_db)) -> Ride:
    return await rides_service.driver_set_status(db, user, ride_id, RideStatus.IN_PROGRESS)


@router.post("/{ride_id}/complete", response_model=RideResponse)
async def complete(
    ride_id: int, user: User = Depends(require_role("driver")), db: AsyncSession = Depends(get_db)
) -> Ride:
    return await rides_service.driver_set_status(db, user, ride_id, RideStatus.COMPLETED)


@router.post("/{ride_id}/cancel", response_model=RideResponse)
async def cancel(
    ride_id: int, user: User = Depends(require_role("rider", "driver")), db: AsyncSession = Depends(get_db)
) -> Ride:
    return await rides_service.cancel(db, user, ride_id)
