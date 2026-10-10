from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import SavedPlace, User

# Rows come back as plain dicts of the table's columns (id, user_id, label, address, lat, lng, created_at).


async def lock_owner(db: AsyncSession, user_id: int) -> None:
    """Blocks until this user's row is ours (FOR UPDATE); the lock lasts until commit or rollback. Plain blocking, like
    wallet.lock_wallet: it is held only for a few statements. Column only, never the entity, so the identity map cannot
    hand back an older copy."""
    await db.execute(select(User.id).where(User.id == user_id).with_for_update())


async def count(db: AsyncSession, user_id: int) -> int:
    result = await db.execute(select(func.count()).where(SavedPlace.user_id == user_id))
    return result.scalar_one()


async def label_exists(db: AsyncSession, user_id: int, label: str, exclude_id: int | None = None) -> bool:
    """Case-insensitive, like the unique index on (user_id, lower(label)). exclude_id is the place being renamed."""
    query = select(SavedPlace.id).where(SavedPlace.user_id == user_id, func.lower(SavedPlace.label) == label.lower())
    if exclude_id is not None:
        query = query.where(SavedPlace.id != exclude_id)
    result = await db.execute(query.limit(1))
    return result.first() is not None


async def insert(db: AsyncSession, user_id: int, label: str, address: str, lat: float, lng: float) -> dict:
    result = await db.execute(
        SavedPlace.__table__.insert()
        .values(user_id=user_id, label=label, address=address, lat=lat, lng=lng)
        .returning(SavedPlace.__table__)
    )
    return dict(result.mappings().one())


async def list_for_user(db: AsyncSession, user_id: int) -> list[dict]:
    result = await db.execute(select(SavedPlace.__table__).where(SavedPlace.user_id == user_id).order_by(SavedPlace.id))
    return [dict(row) for row in result.mappings().all()]


async def get_owned(db: AsyncSession, user_id: int, place_id: int) -> dict | None:
    """Someone else's place and an unknown id look the same: None."""
    result = await db.execute(select(SavedPlace.__table__).where(SavedPlace.id == place_id, SavedPlace.user_id == user_id))
    row = result.mappings().first()
    return dict(row) if row is not None else None


async def rename(db: AsyncSession, place_id: int, label: str) -> dict | None:
    """None when the place is gone: a delete takes no lock, so it can commit between the caller's read and this update."""
    result = await db.execute(
        update(SavedPlace).where(SavedPlace.id == place_id).values(label=label).returning(SavedPlace.__table__)
    )
    row = result.mappings().first()
    return dict(row) if row is not None else None


async def delete_owned(db: AsyncSession, user_id: int, place_id: int) -> bool:
    """False when there was no such place of this user (nothing is deleted then)."""
    result = await db.execute(
        delete(SavedPlace).where(SavedPlace.id == place_id, SavedPlace.user_id == user_id).returning(SavedPlace.id)
    )
    return result.first() is not None
