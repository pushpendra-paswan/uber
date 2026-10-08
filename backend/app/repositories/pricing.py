from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import PricingRule


async def get_rule(db: AsyncSession, vehicle_type: str) -> PricingRule | None:
    result = await db.execute(select(PricingRule).where(PricingRule.vehicle_type == vehicle_type))
    return result.scalar_one_or_none()
