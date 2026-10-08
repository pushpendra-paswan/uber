from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text

from app.database import engine, redis_client
from app.routers import admin, auth, drivers

app = FastAPI(title="Uber Clone")


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

# Mounted last so it does not shadow the API routes above.
app.mount("/", StaticFiles(directory="/frontend", html=True), name="frontend")
