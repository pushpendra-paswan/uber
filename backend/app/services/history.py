from datetime import datetime, timedelta, timezone

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import RideStatus, User
from app.repositories import history as history_repo
from app.services import drivers as drivers_service
from app.services import ratings as ratings_service

HISTORY_DEFAULT_LIMIT = 20
HISTORY_MAX_LIMIT = 50
# A NO_DRIVER_FOUND ride never has a driver, so it is in the rider's list only. Active rides are in neither: the ride view
# shows them.
RIDER_STATUSES = (RideStatus.COMPLETED, RideStatus.CANCELLED, RideStatus.NO_DRIVER_FOUND)
DRIVER_STATUSES = (RideStatus.COMPLETED, RideStatus.CANCELLED)


def check_filters(
    listed: tuple[RideStatus, ...], status: RideStatus | None, since: datetime | None, until: datetime | None, limit: int
) -> tuple[RideStatus, ...]:
    """The statuses to list. 422 for a bad limit, a window that ends before it starts, or a status that is not listed."""
    if not 1 <= limit <= HISTORY_MAX_LIMIT:
        raise HTTPException(status_code=422, detail=f"limit must be between 1 and {HISTORY_MAX_LIMIT}")
    if since is not None and until is not None and until <= since:
        raise HTTPException(status_code=422, detail="until must be after since")
    if status is None:
        return listed
    if status not in listed:
        raise HTTPException(status_code=422, detail="Only finished trips are listed")
    return (status,)


def can_rate(row: dict, now: datetime) -> bool:
    """The same rule as ratings.create_rating, so a row never offers a rating that the endpoint would refuse: COMPLETED, not
    rated by this person yet, and now is not after completed_at plus the window. The window is read here, at call time."""
    if row["status"] != RideStatus.COMPLETED or row["my_rating"] is not None or row["completed_at"] is None:
        return False
    return now <= row["completed_at"] + timedelta(days=ratings_service.RATING_WINDOW_DAYS)


async def list_rider_trips(
    db: AsyncSession, user: User, status: RideStatus | None, since: datetime | None, until: datetime | None, limit: int,
    before_id: int | None,
) -> list[dict]:
    statuses = check_filters(RIDER_STATUSES, status, since, until, limit)
    rows = await history_repo.list_rider(db, user.id, statuses, since, until, limit, before_id)
    now = datetime.now(timezone.utc)
    return [{**row, "can_rate": can_rate(row, now)} for row in rows]


async def list_driver_trips(
    db: AsyncSession, user: User, status: RideStatus | None, since: datetime | None, until: datetime | None, limit: int,
    before_id: int | None,
) -> list[dict]:
    statuses = check_filters(DRIVER_STATUSES, status, since, until, limit)
    # A pending or rejected driver may read their own history too: it is history, not a permission.
    driver = await drivers_service.get_me(db, user)
    rows = await history_repo.list_driver(db, driver.id, user.id, statuses, since, until, limit, before_id)
    now = datetime.now(timezone.utc)
    return [{**row, "can_rate": can_rate(row, now)} for row in rows]
