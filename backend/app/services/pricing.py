from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.repositories import pricing as pricing_repo


async def calculate_fare(db: AsyncSession, distance_m: int, duration_s: int, vehicle_type: str = "economy") -> dict:
    rule = await pricing_repo.get_rule(db, vehicle_type)
    if rule is None:
        raise HTTPException(status_code=503, detail="Pricing is not configured")

    # Integer paise only. Adding half the divisor before // rounds half up.
    distance_fare = (rule.per_km * distance_m + 500) // 1000
    time_fare = (rule.per_min * duration_s + 30) // 60
    subtotal = rule.base_fare + distance_fare + time_fare
    return {
        "base_fare": rule.base_fare,
        "distance_fare": distance_fare,
        "time_fare": time_fare,
        "fare_estimate": max(subtotal, rule.min_fare),
        "minimum_fare_applied": rule.min_fare > subtotal,
    }
