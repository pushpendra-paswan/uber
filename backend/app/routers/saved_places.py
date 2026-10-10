from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import User
from app.schemas import SavedPlaceCreate, SavedPlaceRename, SavedPlaceResponse
from app.security import require_role
from app.services import saved_places as saved_places_service

router = APIRouter(prefix="/saved-places", tags=["saved places"])


@router.get("", response_model=list[SavedPlaceResponse])
async def list_places(user: User = Depends(require_role("rider")), db: AsyncSession = Depends(get_db)) -> list[dict]:
    return await saved_places_service.list_places(db, user)


@router.post("", response_model=SavedPlaceResponse, status_code=201)
async def create_place(
    data: SavedPlaceCreate, user: User = Depends(require_role("rider")), db: AsyncSession = Depends(get_db)
) -> dict:
    return await saved_places_service.create_place(db, user, data)


@router.patch("/{place_id}", response_model=SavedPlaceResponse)
async def rename_place(
    place_id: int, data: SavedPlaceRename, user: User = Depends(require_role("rider")), db: AsyncSession = Depends(get_db)
) -> dict:
    return await saved_places_service.rename_place(db, user, place_id, data)


@router.delete("/{place_id}", status_code=204)
async def delete_place(place_id: int, user: User = Depends(require_role("rider")), db: AsyncSession = Depends(get_db)) -> None:
    await saved_places_service.delete_place(db, user, place_id)
