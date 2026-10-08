import httpx
from fastapi import HTTPException

from app.config import settings

REQUEST_TIMEOUT_SECONDS = 5
SNAP_RADIUS_M = 300  # how far OSRM may move a point to reach a road


async def get_route(pickup_lat: float, pickup_lng: float, dropoff_lat: float, dropoff_lng: float) -> dict:
    # OSRM wants longitude first in the URL and sends GeoJSON as [lng, lat].
    # This is the one place that converts, because Leaflet and the rest of the app use [lat, lng].
    url = f"{settings.osrm_url}/route/v1/driving/{pickup_lng},{pickup_lat};{dropoff_lng},{dropoff_lat}"
    params = {
        "overview": "full",
        "geometries": "geojson",
        "steps": "false",
        "radiuses": f"{SNAP_RADIUS_M};{SNAP_RADIUS_M}",
    }

    # Network calls fail for legitimate reasons, so this is one of the few places with a try/except.
    # The body is read whatever the HTTP status is, because OSRM reports some problems with a 400.
    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS) as client:
            response = await client.get(url, params=params)
        body = response.json()
    except (httpx.HTTPError, ValueError):
        raise HTTPException(status_code=502, detail="Routing is unavailable")

    code = body.get("code") if isinstance(body, dict) else None
    if code == "Ok":
        route = body["routes"][0]
        return {
            "distance_m": round(route["distance"]),
            "duration_s": round(route["duration"]),
            "path": [[lat, lng] for lng, lat in route["geometry"]["coordinates"]],
        }
    if code in ("NoRoute", "NoSegment"):
        raise HTTPException(status_code=422, detail="No route found between these points. Choose points closer to a road.")
    raise HTTPException(status_code=502, detail="Routing is unavailable")
