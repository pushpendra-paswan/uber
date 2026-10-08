import json

from app.database import redis_client

SLOT_KEY = "places:slot"
SLOT_MILLISECONDS = 1100  # a little over 1 second, so we stay under Nominatim's limit


async def get_cached(key: str) -> dict | list | None:
    value = await redis_client.get(key)
    return None if value is None else json.loads(value)


async def set_cached(key: str, value: dict | list, ttl_seconds: int) -> None:
    await redis_client.set(key, json.dumps(value), ex=ttl_seconds)


async def acquire_slot() -> bool:
    return await redis_client.set(SLOT_KEY, 1, nx=True, px=SLOT_MILLISECONDS) is True
