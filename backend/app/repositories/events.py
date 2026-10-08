import json

from redis.asyncio.client import PubSub

from app.config import settings
from app.database import redis_client

# Pub/sub ignores Redis database numbers, so the index is part of the name: the dev backend (database 0)
# and the tests (database 1) would otherwise receive each other's messages.
CHANNEL = f"ws:events:{settings.redis_db}"


async def publish(user_id: int, type: str, data: dict) -> int:
    message = json.dumps({"user_id": user_id, "type": type, "data": data})
    return await redis_client.publish(CHANNEL, message)


async def subscribe() -> PubSub:
    pubsub = redis_client.pubsub(ignore_subscribe_messages=True)
    try:
        await pubsub.subscribe(CHANNEL)
    except BaseException:
        # A failed subscribe still holds a connection; the caller never gets the object to close it.
        await pubsub.aclose()
        raise
    return pubsub
