from fastapi import APIRouter, Depends
from pydantic import AwareDatetime
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import Ride, RideEvent, RideStatus, User
from app.schemas import (
    CancellationFeeResponse,
    EstimateRequest,
    EstimateResponse,
    OtpResponse,
    ReceiptResponse,
    RideCreate,
    RideDriverResponse,
    RideEventResponse,
    RideResponse,
    RiderTripRow,
    StartTripRequest,
)
from app.security import get_current_user, require_role
from app.services import history as history_service
from app.services import receipts as receipts_service
from app.services import rides as rides_service

router = APIRouter(prefix="/rides", tags=["rides"])


@router.post("", response_model=RideResponse, status_code=201)
async def create_ride(
    data: RideCreate, user: User = Depends(require_role("rider")), db: AsyncSession = Depends(get_db)
) -> Ride:
    return await rides_service.create_ride(db, user, data)


# Declared before /{ride_id}, otherwise "estimate" and "active" would be read as a ride id (so is /history below).
@router.post("/estimate", response_model=EstimateResponse)
async def estimate_ride(
    data: EstimateRequest, user: User = Depends(require_role("rider")), db: AsyncSession = Depends(get_db)
) -> dict:
    return await rides_service.estimate_ride(db, data)


@router.get("/active", response_model=RideResponse)
async def get_active_ride(user: User = Depends(require_role("rider", "driver")), db: AsyncSession = Depends(get_db)) -> Ride:
    return await rides_service.get_active(db, user)


# Also before /{ride_id}, otherwise "history" would be read as a ride id. since is inclusive and until exclusive, both compared
# with the request time; both need a timezone (a naive time is a 422).
@router.get("/history", response_model=list[RiderTripRow])
async def list_history(
    status: RideStatus | None = None, since: AwareDatetime | None = None, until: AwareDatetime | None = None,
    limit: int = history_service.HISTORY_DEFAULT_LIMIT, before_id: int | None = None,
    user: User = Depends(require_role("rider")), db: AsyncSession = Depends(get_db),
) -> list[dict]:
    return await history_service.list_rider_trips(db, user, status, since, until, limit, before_id)


@router.get("/{ride_id}", response_model=RideResponse)
async def get_ride(ride_id: int, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> Ride:
    return await rides_service.get_ride(db, user, ride_id)


@router.get("/{ride_id}/driver", response_model=RideDriverResponse)
async def get_ride_driver(
    ride_id: int, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
) -> RideDriverResponse:
    return await rides_service.get_ride_driver(db, user, ride_id)


@router.get("/{ride_id}/otp", response_model=OtpResponse)
async def get_ride_otp(
    ride_id: int, user: User = Depends(require_role("rider")), db: AsyncSession = Depends(get_db)
) -> OtpResponse:
    return await rides_service.get_otp(db, user, ride_id)


@router.get("/{ride_id}/cancellation-fee", response_model=CancellationFeeResponse)
async def get_cancellation_fee(
    ride_id: int, user: User = Depends(require_role("rider")), db: AsyncSession = Depends(get_db)
) -> CancellationFeeResponse:
    return await rides_service.get_cancellation_fee(db, user, ride_id)


@router.get("/{ride_id}/receipt", response_model=ReceiptResponse)
async def get_ride_receipt(
    ride_id: int, user: User = Depends(require_role("rider")), db: AsyncSession = Depends(get_db)
) -> dict:
    return await receipts_service.get_receipt(db, user, ride_id)


@router.get("/{ride_id}/events", response_model=list[RideEventResponse])
async def get_ride_events(
    ride_id: int, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
) -> list[RideEvent]:
    return await rides_service.get_events(db, user, ride_id)


@router.post("/{ride_id}/arrive", response_model=RideResponse)
async def arrive(ride_id: int, user: User = Depends(require_role("driver")), db: AsyncSession = Depends(get_db)) -> Ride:
    return await rides_service.driver_set_status(db, user, ride_id, RideStatus.DRIVER_ARRIVED)


@router.post("/{ride_id}/start", response_model=RideResponse)
async def start(
    ride_id: int, data: StartTripRequest, user: User = Depends(require_role("driver")), db: AsyncSession = Depends(get_db)
) -> Ride:
    return await rides_service.driver_set_status(db, user, ride_id, RideStatus.IN_PROGRESS, data.otp)


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
