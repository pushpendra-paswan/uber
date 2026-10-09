from datetime import datetime

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import User
from app.repositories import drivers as drivers_repo
from app.repositories import earnings as earnings_repo
from app.services import drivers as drivers_service

ENTRIES_DEFAULT_LIMIT = 20
ENTRIES_MAX_LIMIT = 100


def fold_rows(rows: list) -> dict:
    """Turns the rows of the one grouped query (kind, method, rides, gross, platform_fee, driver_earning) into an answer.
    Every method counts in `total`; only cash and wallet get a bucket of their own (card counts in `total` only).

    Whose money is whose: on a cash ride the driver collected the fare and owes the platform its fee; on a wallet ride the
    platform collected the fare and owes the driver the earning. The balance is derived from the rows, never stored."""
    buckets = {name: {"rides": 0, "gross": 0, "platform_fee": 0, "driver_earning": 0} for name in ("total", "cash", "wallet")}
    trips = cancellation_fees = 0
    for row in rows:
        if row.kind == "trip":
            trips += row.rides
        else:
            cancellation_fees += row.rides
        for name in ("total", row.method.value):
            if name in buckets:
                buckets[name]["rides"] += row.rides
                buckets[name]["gross"] += row.gross
                buckets[name]["platform_fee"] += row.platform_fee
                buckets[name]["driver_earning"] += row.driver_earning
    owed_to_driver = buckets["wallet"]["driver_earning"]
    owed_by_driver = buckets["cash"]["platform_fee"]
    return {
        "trips": trips,
        "cancellation_fees": cancellation_fees,
        **buckets,
        "settlement": {"owed_to_driver": owed_to_driver, "owed_by_driver": owed_by_driver, "net": owed_to_driver - owed_by_driver},
    }


async def get_driver_summary(db: AsyncSession, user: User, since: datetime | None, until: datetime | None) -> dict:
    if since is not None and until is not None and until <= since:
        raise HTTPException(status_code=422, detail="until must be after since")
    # A pending or rejected driver may read their own earnings too: they are history, not a permission.
    driver = await drivers_service.get_me(db, user)
    rows = await earnings_repo.summarize(db, since, until, driver.id)
    return {"since": since, "until": until, **fold_rows(rows)}


async def list_entries(
    db: AsyncSession, user: User, since: datetime | None, until: datetime | None, limit: int, before_id: int | None
) -> list[dict]:
    if not 1 <= limit <= ENTRIES_MAX_LIMIT:
        raise HTTPException(status_code=422, detail=f"limit must be between 1 and {ENTRIES_MAX_LIMIT}")
    if since is not None and until is not None and until <= since:
        raise HTTPException(status_code=422, detail="until must be after since")
    driver = await drivers_service.get_me(db, user)
    return await earnings_repo.list_for_driver(db, driver.id, since, until, limit, before_id)


async def get_admin_driver_summary(db: AsyncSession, driver_id: int, since: datetime | None, until: datetime | None) -> dict:
    if since is not None and until is not None and until <= since:
        raise HTTPException(status_code=422, detail="until must be after since")
    if await drivers_repo.get_by_id(db, driver_id) is None:
        raise HTTPException(status_code=404, detail="Driver not found")
    rows = await earnings_repo.summarize(db, since, until, driver_id)
    return {"since": since, "until": until, **fold_rows(rows)}


async def get_revenue(db: AsyncSession, since: datetime | None, until: datetime | None) -> dict:
    """The platform's revenue is total.platform_fee. The settlement is across all drivers."""
    if since is not None and until is not None and until <= since:
        raise HTTPException(status_code=422, detail="until must be after since")
    rows = await earnings_repo.summarize(db, since, until)
    return {"since": since, "until": until, **fold_rows(rows)}
