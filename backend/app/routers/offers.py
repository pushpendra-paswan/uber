from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import Ride, User
from app.schemas import RideResponse
from app.security import require_role
from app.services import offers as offers_service

router = APIRouter(prefix="/offers", tags=["offers"])


@router.post("/{offer_id}/accept", response_model=RideResponse)
async def accept(offer_id: int, user: User = Depends(require_role("driver")), db: AsyncSession = Depends(get_db)) -> Ride:
    return await offers_service.accept(db, user, offer_id)


@router.post("/{offer_id}/reject", status_code=204)
async def reject(offer_id: int, user: User = Depends(require_role("driver")), db: AsyncSession = Depends(get_db)) -> None:
    await offers_service.reject(db, user, offer_id)
