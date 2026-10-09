from fastapi import APIRouter, Depends, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import Driver, User, VerificationStatus
from app.schemas import AdjustRequest, DriverResponse, IdempotencyKey, SurgeSnapshotResponse, WalletEntryResponse
from app.security import require_role
from app.services import drivers as drivers_service
from app.services import pricing as pricing_service
from app.services import wallet as wallet_service

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


@router.get("/surge", response_model=SurgeSnapshotResponse)
async def get_surge(refresh: bool = False, db: AsyncSession = Depends(get_db)) -> dict:
    return await pricing_service.get_surge_zones(db, refresh)


@router.post("/wallets/{user_id}/adjust", response_model=WalletEntryResponse, status_code=201)
async def adjust_wallet(
    user_id: int, data: AdjustRequest, response: Response, key: IdempotencyKey,
    admin: User = Depends(require_role("admin")), db: AsyncSession = Depends(get_db),
) -> dict:
    entry, created = await wallet_service.adjust(db, admin, user_id, key, data)
    if not created:
        response.status_code = 200
    return entry
