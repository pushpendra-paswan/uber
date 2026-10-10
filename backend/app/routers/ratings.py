from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import User
from app.schemas import RatingCreate, RatingResponse, RatingStatusResponse, RatingSummaryResponse
from app.security import require_role
from app.services import ratings as ratings_service

# No prefix: the routes sit under /rides/{id} and /ratings, so the paths are written out in full.
router = APIRouter(tags=["ratings"])


@router.post("/rides/{ride_id}/rating", response_model=RatingResponse, status_code=201)
async def create_rating(
    ride_id: int, data: RatingCreate, user: User = Depends(require_role("rider", "driver")), db: AsyncSession = Depends(get_db)
) -> dict:
    return await ratings_service.create_rating(db, user, ride_id, data)


@router.get("/rides/{ride_id}/rating", response_model=RatingStatusResponse)
async def get_rating_status(
    ride_id: int, user: User = Depends(require_role("rider", "driver")), db: AsyncSession = Depends(get_db)
) -> dict:
    return await ratings_service.get_rating_status(db, user, ride_id)


@router.get("/ratings/me", response_model=RatingSummaryResponse)
async def get_my_summary(user: User = Depends(require_role("rider", "driver")), db: AsyncSession = Depends(get_db)) -> dict:
    return await ratings_service.get_my_summary(db, user)
