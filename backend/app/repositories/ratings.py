from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Rating, RatingSummary


async def insert_if_new(db: AsyncSession, ride_id: int, from_user_id: int, to_user_id: int, score: int, comment: str | None) -> dict | None:
    """The new rating (id, ride_id, score, comment, created_at), or None when this user already rated this ride."""
    result = await db.execute(
        insert(Rating)
        .values(ride_id=ride_id, from_user_id=from_user_id, to_user_id=to_user_id, score=score, comment=comment)
        .on_conflict_do_nothing(index_elements=[Rating.ride_id, Rating.from_user_id])
        .returning(Rating.id, Rating.ride_id, Rating.score, Rating.comment, Rating.created_at)
    )
    row = result.mappings().first()
    return dict(row) if row is not None else None


async def add_to_summary(db: AsyncSession, user_id: int, score: int) -> None:
    """ONE atomic upsert, never a read followed by a write, so two ratings of one user cannot lose an update."""
    await db.execute(
        insert(RatingSummary)
        .values(user_id=user_id, rating_count=1, rating_total=score)
        .on_conflict_do_update(
            index_elements=[RatingSummary.user_id],
            set_={
                "rating_count": RatingSummary.rating_count + 1,
                "rating_total": RatingSummary.rating_total + score,
                "updated_at": func.now(),
            },
        )
    )


async def get_summary(db: AsyncSession, user_id: int) -> tuple[int, int]:
    """(count, total). (0, 0) for a user nobody rated: reading never creates a row."""
    result = await db.execute(
        select(RatingSummary.rating_count, RatingSummary.rating_total).where(RatingSummary.user_id == user_id)
    )
    row = result.first()
    return (row.rating_count, row.rating_total) if row is not None else (0, 0)


async def get_by_ride_and_rater(db: AsyncSession, ride_id: int, from_user_id: int) -> dict | None:
    result = await db.execute(
        select(Rating.id, Rating.ride_id, Rating.score, Rating.comment, Rating.created_at)
        .where(Rating.ride_id == ride_id, Rating.from_user_id == from_user_id)
    )
    row = result.mappings().first()
    return dict(row) if row is not None else None


async def list_for_admin(db: AsyncSession, user_id: int | None, max_score: int | None, limit: int, before_id: int | None) -> list[dict]:
    query = select(
        Rating.id, Rating.ride_id, Rating.from_user_id, Rating.to_user_id, Rating.score, Rating.comment, Rating.created_at
    )
    if user_id is not None:
        query = query.where(Rating.to_user_id == user_id)
    if max_score is not None:
        query = query.where(Rating.score <= max_score)
    if before_id is not None:
        query = query.where(Rating.id < before_id)
    result = await db.execute(query.order_by(Rating.id.desc()).limit(limit))
    return [dict(row) for row in result.mappings().all()]
