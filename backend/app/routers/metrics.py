import hmac

from fastapi import APIRouter, Header, HTTPException, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from app.config import settings

router = APIRouter()


# Not an API for users: a user JWT never works here. No token configured means the endpoint does not exist.
@router.get("/metrics", include_in_schema=False)
async def get_metrics(authorization: str | None = Header(default=None)) -> Response:
    if not settings.metrics_token:
        raise HTTPException(status_code=404, detail="Not Found")
    scheme, _, token = (authorization or "").partition(" ")
    # Bytes, because compare_digest refuses text with non-ASCII characters.
    if scheme != "Bearer" or not hmac.compare_digest(token.encode(), settings.metrics_token.encode()):
        return Response("Unauthorized", status_code=401, headers={"WWW-Authenticate": "Bearer"})
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
