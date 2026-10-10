import asyncio
import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from redis.exceptions import RedisError
from sqlalchemy import text

from app.config import settings
from app.database import engine, redis_client
from app.observability.hooks import loop_lag_monitor, refresh_gauges_forever, start_background_task
from app.observability.logs import setup_logging
from app.observability.middleware import observability_middleware
from app.routers import admin, auth, drivers, metrics, offers, payments, places, ratings, rides, saved_places, websocket
from app.services import offers as offers_service
from app.services.payments import TEST_KEY_PREFIXES

setup_logging()

# uvicorn's logger, because it is the one that has a handler and prints INFO.
logger = logging.getLogger("uvicorn.error")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    # One line about payments, never the values themselves.
    if not settings.stripe_secret_key:
        logger.info("payments: stripe not configured")
    elif not settings.stripe_secret_key.startswith(TEST_KEY_PREFIXES):
        logger.error("payments: stripe REJECTED: key is not a test key (only sk_test_ and rk_test_ keys are used)")
    else:
        webhook = "webhook secret set" if settings.stripe_webhook_secret else "webhook secret NOT set"
        logger.info("payments: stripe configured (test key), %s", webhook)

    # Starts even when Redis or Postgres is down: both tasks keep retrying in the background.
    tasks = [
        start_background_task("ws_listener", websocket.listen_for_events()),
        start_background_task("sweeper", offers_service.sweep_forever()),
        start_background_task("gauges", refresh_gauges_forever()),
        start_background_task("loop_lag", loop_lag_monitor()),
    ]
    yield
    for task in tasks:
        task.cancel()
    for task in tasks:
        try:
            await task
        except asyncio.CancelledError:
            pass


app = FastAPI(title="Uber Clone", lifespan=lifespan)
app.add_middleware(observability_middleware)


@app.exception_handler(RedisError)
async def redis_unavailable(request: Request, error: RedisError) -> JSONResponse:
    return JSONResponse({"detail": "Cache is unavailable"}, status_code=503)


@app.get("/health")
async def health() -> JSONResponse:
    result = {"status": "ok", "postgres": "ok", "redis": "ok"}

    try:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
    except Exception:
        result["postgres"] = "error"

    try:
        await redis_client.ping()
    except Exception:
        result["redis"] = "error"

    if "error" in (result["postgres"], result["redis"]):
        result["status"] = "error"
        return JSONResponse(result, status_code=503)
    return JSONResponse(result)


app.include_router(auth.router)
app.include_router(drivers.router)
app.include_router(admin.router)
app.include_router(rides.router)
app.include_router(offers.router)
app.include_router(payments.router)
app.include_router(places.router)
app.include_router(ratings.router)
app.include_router(saved_places.router)
app.include_router(websocket.router)
app.include_router(metrics.router)

# Mounted last so it does not shadow the API routes above.
app.mount("/", StaticFiles(directory="/frontend", html=True), name="frontend")
