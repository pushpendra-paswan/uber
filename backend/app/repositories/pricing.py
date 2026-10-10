import json

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import redis_client
from app.models import ACTIVE_RIDE_STATUSES, PricingRule, PricingRuleChange, Ride, User

SNAPSHOT_KEY = "surge:snapshot"


async def get_rule(db: AsyncSession, vehicle_type: str) -> PricingRule | None:
    result = await db.execute(select(PricingRule).where(PricingRule.vehicle_type == vehicle_type))
    return result.scalar_one_or_none()


# The admin pricing editor (M6.2). These flush but never commit, and they use no rules.


async def list_rules(db: AsyncSession) -> list[dict]:
    result = await db.execute(
        select(*PricingRule.__table__.c, User.name.label("updated_by_name"))
        .outerjoin(User, User.id == PricingRule.updated_by)
        .order_by(PricingRule.vehicle_type)
    )
    return [dict(row) for row in result.mappings().all()]


async def lock_rule(db: AsyncSession, vehicle_type: str) -> int | None:
    """Takes the rule's row (FOR UPDATE) until commit or rollback and returns its id, or None for an unknown type.
    Column-only, so the identity map cannot return an older copy. The caller reads the rule and checks its version in
    separate statements AFTER the lock, which is what sees the previous holder's commit."""
    result = await db.execute(select(PricingRule.id).where(PricingRule.vehicle_type == vehicle_type).with_for_update())
    return result.scalar_one_or_none()


async def update_rule(db: AsyncSession, rule_id: int, values: dict, version_after: int, actor_user_id: int) -> None:
    await db.execute(
        update(PricingRule)
        .where(PricingRule.id == rule_id)
        .values(**values, version=version_after, updated_at=func.now(), updated_by=actor_user_id)
        .execution_options(synchronize_session=False)
    )
    await db.flush()


async def insert_change(
    db: AsyncSession, rule_id: int, actor_user_id: int, version_before: int, version_after: int, changes: list[dict]
) -> None:
    db.add(
        PricingRuleChange(
            rule_id=rule_id, actor_user_id=actor_user_id, version_before=version_before, version_after=version_after, changes=changes
        )
    )
    await db.flush()


async def list_changes(db: AsyncSession, rule_id: int, limit: int) -> list[dict]:
    result = await db.execute(
        select(
            PricingRuleChange.id, User.name.label("actor_name"), PricingRuleChange.version_before,
            PricingRuleChange.version_after, PricingRuleChange.changes, PricingRuleChange.created_at,
        )
        .join(User, User.id == PricingRuleChange.actor_user_id)
        .where(PricingRuleChange.rule_id == rule_id)
        .order_by(PricingRuleChange.id.desc())
        .limit(limit)
    )
    return [dict(row) for row in result.mappings().all()]


async def count_active_rides(db: AsyncSession) -> int:
    result = await db.execute(select(func.count()).select_from(Ride).where(Ride.status.in_(ACTIVE_RIDE_STATUSES)))
    return result.scalar_one()


# The two Redis functions raise RedisError; the service decides what a failure means.


async def get_snapshot() -> dict | None:
    value = await redis_client.get(SNAPSHOT_KEY)
    return None if value is None else json.loads(value)


async def save_snapshot(snapshot: dict, ttl_seconds: int) -> None:
    await redis_client.set(SNAPSHOT_KEY, json.dumps(snapshot), ex=ttl_seconds)
