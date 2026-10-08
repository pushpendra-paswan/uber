from fastapi import APIRouter, Depends, Query

from app.models import User
from app.schemas import MapConfigResponse, PlaceResponse
from app.security import get_current_user
from app.services import places as places_service

router = APIRouter(prefix="/places", tags=["places"])


@router.get("/map-config", response_model=MapConfigResponse)
async def get_map_config(user: User = Depends(get_current_user)) -> MapConfigResponse:
    return places_service.get_map_config()


@router.get("/search", response_model=list[PlaceResponse])
async def search(
    q: str = Query(min_length=3, max_length=200), user: User = Depends(get_current_user)
) -> list[PlaceResponse]:
    return await places_service.search(q)


@router.get("/reverse", response_model=PlaceResponse)
async def reverse(
    lat: float = Query(ge=-90, le=90), lng: float = Query(ge=-180, le=180), user: User = Depends(get_current_user)
) -> PlaceResponse:
    return await places_service.reverse(lat, lng)
