import json

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import redis_client
from app.models import PricingRule

SNAPSHOT_KEY = "surge:snapshot"


async def get_rule(db: AsyncSession, vehicle_type: str) -> PricingRule | None:
    result = await db.execute(select(PricingRule).where(PricingRule.vehicle_type == vehicle_type))
    return result.scalar_one_or_none()


# The two Redis functions raise RedisError; the service decides what a failure means.


async def get_snapshot() -> dict | None:
    value = await redis_client.get(SNAPSHOT_KEY)
    return None if value is None else json.loads(value)


async def save_snapshot(snapshot: dict, ttl_seconds: int) -> None:
    await redis_client.set(SNAPSHOT_KEY, json.dumps(snapshot), ex=ttl_seconds)
