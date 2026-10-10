import logging

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import User
from app.repositories import saved_places as saved_places_repo
from app.schemas import SavedPlaceCreate, SavedPlaceRename
from app.utils.geo import is_inside_bounds

MAX_SAVED_PLACES = 10

# uvicorn's logger, because it is the one that has a handler and prints INFO.
logger = logging.getLogger("uvicorn.error")


async def list_places(db: AsyncSession, user: User) -> list[dict]:
    return await saved_places_repo.list_for_user(db, user.id)


async def create_place(db: AsyncSession, user: User, data: SavedPlaceCreate) -> dict:
    # Checked before the lock: a refused point costs no wait. The CITY_* settings are read here, at call time.
    if not is_inside_bounds(data.lat, data.lng, settings.city_south, settings.city_west, settings.city_north, settings.city_east):
        raise HTTPException(status_code=422, detail="Location is outside the service area")

    # Lock, then check as separate statements, then write, then commit. The lock is the owner's user row (FOR UPDATE), a leaf
    # lock: it is held only for the checks, the insert and the commit, and nothing else is locked while it is held. Under
    # READ COMMITTED the count and the label check start after the lock is granted, so they see what the previous holder
    # committed. The database cannot count, so this lock IS what enforces the cap; the unique index on (user_id, lower(label))
    # is the safety net behind the label check. A violation of it would mean the lock is missing, so it is not caught.
    await saved_places_repo.lock_owner(db, user.id)
    if await saved_places_repo.count(db, user.id) >= MAX_SAVED_PLACES:
        raise HTTPException(status_code=409, detail=f"You can save up to {MAX_SAVED_PLACES} places. Delete one first.")
    if await saved_places_repo.label_exists(db, user.id, data.label):
        raise HTTPException(status_code=409, detail="You already have a place with that name.")
    place = await saved_places_repo.insert(db, user.id, data.label, data.address, data.lat, data.lng)
    await db.commit()

    logger.info("Saved place %s created by user %s", place["id"], user.id)
    return place


async def rename_place(db: AsyncSession, user: User, place_id: int, data: SavedPlaceRename) -> dict:
    # Same lock, same order as create_place: lock, then check, then write, then commit.
    await saved_places_repo.lock_owner(db, user.id)
    place = await saved_places_repo.get_owned(db, user.id, place_id)
    if place is None:
        raise HTTPException(status_code=404, detail="Place not found")
    if place["label"] == data.label:
        return place
    # The place itself is left out of the check, so a change of letter case only ("home" to "Home") is allowed.
    if await saved_places_repo.label_exists(db, user.id, data.label, exclude_id=place_id):
        raise HTTPException(status_code=409, detail="You already have a place with that name.")
    place = await saved_places_repo.rename(db, place_id, data.label)
    if place is None:  # deleted by a request that does not lock, between the read above and the update
        raise HTTPException(status_code=404, detail="Place not found")
    await db.commit()

    logger.info("Saved place %s renamed by user %s", place_id, user.id)
    return place


async def delete_place(db: AsyncSession, user: User, place_id: int) -> None:
    # No lock: one statement deletes only this rider's own row, and removing a row can never break the cap or the labels.
    if not await saved_places_repo.delete_owned(db, user.id, place_id):
        raise HTTPException(status_code=404, detail="Place not found")
    await db.commit()

    logger.info("Saved place %s deleted by user %s", place_id, user.id)
