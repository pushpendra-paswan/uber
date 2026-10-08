import httpx
from fastapi import HTTPException

from app.config import settings
from app.repositories import places as places_repo
from app.schemas import MapConfigResponse, PlaceResponse

CACHE_TTL_SECONDS = 86400
RESULT_LIMIT = 5
REQUEST_TIMEOUT_SECONDS = 5


# Used by search and reverse. Every uncached call to Nominatim goes through here,
# because the public server allows at most 1 request per second.
async def call_nominatim(path: str, params: dict) -> dict | list:
    if not await places_repo.acquire_slot():
        raise HTTPException(
            status_code=429, detail="Place search is busy, try again in a second", headers={"Retry-After": "1"}
        )

    # Network calls fail for legitimate reasons, so this is one of the few places with a try/except.
    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS) as client:
            response = await client.get(
                settings.nominatim_url + path,
                params=params,
                headers={"User-Agent": settings.nominatim_user_agent},
            )
    except httpx.HTTPError:
        raise HTTPException(status_code=502, detail="Place search is unavailable")
    if response.status_code != 200:
        raise HTTPException(status_code=502, detail="Place search is unavailable")
    return response.json()


def get_map_config() -> MapConfigResponse:
    return MapConfigResponse(
        city_name=settings.city_name,
        center_lat=settings.city_center_lat,
        center_lng=settings.city_center_lng,
        zoom=settings.map_zoom,
        south=settings.city_south,
        west=settings.city_west,
        north=settings.city_north,
        east=settings.city_east,
    )


async def search(query: str) -> list[PlaceResponse]:
    normalized = " ".join(query.lower().split())
    if len(normalized) < 3:
        raise HTTPException(status_code=422, detail="Type at least 3 characters to search")

    key = f"places:search:{normalized}"
    cached = await places_repo.get_cached(key)
    if cached is not None:
        return cached

    results = await call_nominatim(
        "/search",
        {
            "q": normalized,
            "format": "jsonv2",
            "limit": RESULT_LIMIT,
            "viewbox": f"{settings.city_west},{settings.city_north},{settings.city_east},{settings.city_south}",
            "bounded": 1,
        },
    )
    # Nominatim sends lat and lon as strings.
    places = [{"display_name": item["display_name"], "lat": float(item["lat"]), "lng": float(item["lon"])} for item in results]
    await places_repo.set_cached(key, places, CACHE_TTL_SECONDS)
    return places


async def reverse(lat: float, lng: float) -> PlaceResponse:
    if not (settings.city_south <= lat <= settings.city_north and settings.city_west <= lng <= settings.city_east):
        raise HTTPException(status_code=422, detail="Location is outside the service area")

    # 4 decimals is about 11 m, so clicks next to each other share one cache entry.
    key = f"places:reverse:{lat:.4f}:{lng:.4f}"
    cached = await places_repo.get_cached(key)
    if cached is not None:
        return PlaceResponse(display_name=cached["display_name"], lat=lat, lng=lng)

    result = await call_nominatim("/reverse", {"lat": lat, "lon": lng, "format": "jsonv2", "zoom": 18})
    if "error" in result:
        raise HTTPException(status_code=404, detail="No address found for this location")

    await places_repo.set_cached(key, {"display_name": result["display_name"]}, CACHE_TTL_SECONDS)
    # The clicked point, not the road or building Nominatim snapped it to.
    return PlaceResponse(display_name=result["display_name"], lat=lat, lng=lng)
