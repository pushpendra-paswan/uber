import logging
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import RideStatus, User, UserRole
from app.repositories import drivers as drivers_repo
from app.repositories import ratings as ratings_repo
from app.schemas import RatingCreate
from app.services import rides
from app.utils.ratings import average

RATING_WINDOW_DAYS = 7
ADMIN_LIST_DEFAULT_LIMIT = 20
ADMIN_LIST_MAX_LIMIT = 100

# uvicorn's logger, because it is the one that has a handler and prints INFO.
logger = logging.getLogger("uvicorn.error")


async def create_rating(db: AsyncSession, user: User, ride_id: int, data: RatingCreate) -> dict:
    ride = await rides.load_ride_for_user(db, user, ride_id)
    if ride.status != RideStatus.COMPLETED:
        raise HTTPException(status_code=409, detail="You can only rate completed trips")
    # The constant is read here, at call time. Exactly at the end of the window is still allowed.
    if datetime.now(timezone.utc) > ride.completed_at + timedelta(days=RATING_WINDOW_DAYS):
        raise HTTPException(status_code=409, detail="The rating period for this trip has ended")

    # Ratings go to users, not to drivers rows: the driver's user id is not the driver id.
    to_user_id = await drivers_repo.get_user_id(db, ride.driver_id) if user.role == UserRole.rider else ride.rider_id

    rating = await ratings_repo.insert_if_new(db, ride.id, user.id, to_user_id, data.score, data.comment)
    if rating is None:
        raise HTTPException(status_code=409, detail="You have already rated this trip")
    # No ride lock is needed: a COMPLETED ride and its completed_at never change. The summary row is a leaf lock, taken
    # as the LAST write before the commit: nothing else is locked while it is held, so it cannot be part of a cycle.
    # Concurrent ratings of one popular user wait here for the previous holder's commit, and nobody loses an update
    # because the upsert adds to the stored values instead of reading them first.
    await ratings_repo.add_to_summary(db, to_user_id, data.score)
    await db.commit()

    logger.info("Rating %s created: ride %s, user %s rated user %s, score %s", rating["id"], ride.id, user.id, to_user_id, data.score)
    return rating


async def get_rating_status(db: AsyncSession, user: User, ride_id: int) -> dict:
    ride = await rides.load_ride_for_user(db, user, ride_id)
    if ride.status != RideStatus.COMPLETED:
        return {"can_rate": False, "reason": "not_completed", "expires_at": None, "mine": None}

    expires_at = ride.completed_at + timedelta(days=RATING_WINDOW_DAYS)
    mine = await ratings_repo.get_by_ride_and_rater(db, ride.id, user.id)
    if mine is not None:
        return {"can_rate": False, "reason": "already_rated", "expires_at": expires_at, "mine": mine}
    if datetime.now(timezone.utc) > expires_at:
        return {"can_rate": False, "reason": "window_closed", "expires_at": expires_at, "mine": None}
    return {"can_rate": True, "reason": None, "expires_at": expires_at, "mine": None}


async def get_my_summary(db: AsyncSession, user: User) -> dict:
    count, total = await ratings_repo.get_summary(db, user.id)
    return {"count": count, "average": average(count, total)}


async def list_for_admin(db: AsyncSession, user_id: int | None, max_score: int | None, limit: int, before_id: int | None) -> list[dict]:
    if not 1 <= limit <= ADMIN_LIST_MAX_LIMIT:
        raise HTTPException(status_code=422, detail=f"limit must be between 1 and {ADMIN_LIST_MAX_LIMIT}")
    if max_score is not None and not 1 <= max_score <= 5:
        raise HTTPException(status_code=422, detail="max_score must be between 1 and 5")
    return await ratings_repo.list_for_admin(db, user_id, max_score, limit, before_id)
