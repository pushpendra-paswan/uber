from datetime import datetime

from sqlalchemy import Row, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import OfferStatus, RideOffer


async def create(db: AsyncSession, ride_id: int, driver_id: int, pickup_distance_m: int, expires_at: datetime) -> RideOffer:
    offer = RideOffer(
        ride_id=ride_id,
        driver_id=driver_id,
        status=OfferStatus.PENDING,
        pickup_distance_m=pickup_distance_m,
        expires_at=expires_at,
    )
    db.add(offer)
    await db.flush()
    return offer


async def get_ride_and_driver_ids(db: AsyncSession, offer_id: int) -> Row | None:
    # Columns, not the entity: the locked read of the entity that follows must be its first load in the session,
    # otherwise the identity map would hand back the earlier, unlocked copy and the status check would be stale.
    result = await db.execute(select(RideOffer.ride_id, RideOffer.driver_id).where(RideOffer.id == offer_id))
    return result.first()


async def get_by_id(db: AsyncSession, offer_id: int, for_update: bool = False) -> RideOffer | None:
    query = select(RideOffer).where(RideOffer.id == offer_id)
    if for_update:
        query = query.with_for_update()
    result = await db.execute(query)
    return result.scalar_one_or_none()


async def list_driver_ids_for_ride(db: AsyncSession, ride_id: int) -> set[int]:
    result = await db.execute(select(RideOffer.driver_id).where(RideOffer.ride_id == ride_id))
    return set(result.scalars().all())


async def get_pending_for_ride(db: AsyncSession, ride_id: int) -> RideOffer | None:
    result = await db.execute(select(RideOffer).where(RideOffer.ride_id == ride_id, RideOffer.status == OfferStatus.PENDING))
    return result.scalar_one_or_none()


async def get_oldest_pending_for_driver(db: AsyncSession, driver_id: int, now: datetime) -> RideOffer | None:
    result = await db.execute(
        select(RideOffer)
        .where(RideOffer.driver_id == driver_id, RideOffer.status == OfferStatus.PENDING, RideOffer.expires_at > now)
        .order_by(RideOffer.created_at, RideOffer.id)
        .limit(1)
    )
    return result.scalar_one_or_none()


async def list_due(db: AsyncSession, now: datetime, limit: int) -> list[Row]:
    result = await db.execute(
        select(RideOffer.id, RideOffer.ride_id)
        .where(RideOffer.status == OfferStatus.PENDING, RideOffer.expires_at <= now)
        .order_by(RideOffer.expires_at)
        .limit(limit)
    )
    return list(result.all())


async def set_status(db: AsyncSession, offer: RideOffer, status: OfferStatus, responded_at: datetime | None = None) -> None:
    offer.status = status
    offer.responded_at = responded_at
    await db.flush()
