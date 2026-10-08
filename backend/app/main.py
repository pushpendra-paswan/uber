import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from redis.exceptions import RedisError
from sqlalchemy import text

from app.database import engine, redis_client
from app.routers import admin, auth, drivers, places, rides, websocket


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    # Starts even when Redis is down: the listener keeps retrying in the background.
    listener = asyncio.create_task(websocket.listen_for_events())
    yield
    listener.cancel()
    try:
        await listener
    except asyncio.CancelledError:
        pass


app = FastAPI(title="Uber Clone", lifespan=lifespan)


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
app.include_router(places.router)
app.include_router(websocket.router)

# Mounted last so it does not shadow the API routes above.
app.mount("/", StaticFiles(directory="/frontend", html=True), name="frontend")
