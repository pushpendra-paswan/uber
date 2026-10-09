import asyncio
import collections
import time
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app import database
from app.models import ACTIVE_RIDE_STATUSES, OfferStatus, Ride, RideOffer, RideStatus
from app.repositories import drivers as drivers_repo
from app.repositories import offers as offers_repo
from app.repositories import rides as rides_repo
from conftest import widen
from test_matching import offers_of, online_at  # noqa: F401  (online_at is a fixture)
from test_rides import RIDE_BODY


@pytest_asyncio.fixture
async def hold_row(test_engine):
    """Returns a function that locks a row (FOR UPDATE) in a separate session and returns that session. The lock is
    released by `await session.rollback()` or, at the latest, when the test ends: a failing test must never leave a
    lock behind, or the next test's TRUNCATE would wait for it forever."""
    sessions = []

    async def hold(table: str, row_id: int):
        session = async_sessionmaker(test_engine)()
        sessions.append(session)
        await session.execute(text(f"SELECT id FROM {table} WHERE id = :id FOR UPDATE"), {"id": row_id})
        return session

    yield hold
    for session in sessions:
        await session.rollback()
        await session.close()


async def pending_offer_drivers(db) -> list[int]:
    """The driver of every live PENDING offer. Columns only, so the session's identity map cannot hide a change."""
    result = await db.execute(
        select(RideOffer.driver_id).where(RideOffer.status == OfferStatus.PENDING, RideOffer.expires_at > func.now())
    )
    return sorted(result.scalars().all())


async def post_ride(client, who: dict):
    return await client.post("/rides", json=RIDE_BODY, headers=who["headers"])


async def assert_no_lock_left(test_engine, driver_ids: list[int], user_ids: list[int]) -> None:
    """A separate session can take the same rows with NOWAIT (it raises if a lock is still held)."""
    async with async_sessionmaker(test_engine)() as other:
        await other.execute(text("SELECT id FROM drivers WHERE id = ANY(:ids) FOR UPDATE NOWAIT"), {"ids": driver_ids})
        await other.execute(text("SELECT id FROM users WHERE id = ANY(:ids) FOR UPDATE NOWAIT"), {"ids": user_ids})
        await other.rollback()


async def test_one_driver_two_riders_gives_one_offer(client, db, make_user, online_at, monkeypatch):
    driver = await online_at(500)
    riders = [await make_user("rider") for _ in range(2)]
    widen(monkeypatch, drivers_repo, "get_available_ids", 0.05)

    responses = await asyncio.gather(*[post_ride(client, who) for who in riders])

    assert [response.status_code for response in responses] == [201, 201]
    assert sorted(response.json()["status"] for response in responses) == ["NO_DRIVER_FOUND", "REQUESTED"]
    assert await pending_offer_drivers(db) == [driver["driver"].id]


async def test_a_stale_prefilter_cannot_create_a_second_offer(client, db, make_user, online_at, monkeypatch):
    """The check after the lock is what matters. Rider 2 has already been told "the driver is free" when rider 1 gets
    the offer and commits. Rider 2 then takes the lock; only a fresh check can notice that the driver is taken."""
    driver = await online_at(500)
    rider_1, rider_2 = await make_user("rider"), await make_user("rider")
    reached, gate = asyncio.Event(), asyncio.Event()
    real = drivers_repo.get_available_ids
    calls = 0

    async def gated(*args, **kwargs):
        nonlocal calls
        result = await real(*args, **kwargs)  # the real (free) answer
        calls += 1
        if calls == 1:
            reached.set()
            await gate.wait()
        return result

    monkeypatch.setattr(drivers_repo, "get_available_ids", gated)

    second = asyncio.create_task(post_ride(client, rider_2))
    await asyncio.wait_for(reached.wait(), 5)
    first = await post_ride(client, rider_1)  # runs to completion; its calls are not gated
    gate.set()
    second = await second

    assert first.json()["status"] == "REQUESTED"
    assert second.json()["status"] == "NO_DRIVER_FOUND"
    assert await pending_offer_drivers(db) == [driver["driver"].id]


async def test_eight_riders_three_drivers_gives_exactly_three_offers(client, db, make_user, online_at, monkeypatch):
    drivers = [await online_at(meters) for meters in (300, 900, 1500)]
    riders = [await make_user("rider") for _ in range(8)]
    widen(monkeypatch, drivers_repo, "get_available_ids", 0.02)

    for _ in range(3):
        responses = await asyncio.gather(*[post_ride(client, who) for who in riders])

        assert [response.status_code for response in responses] == [201] * 8
        statuses = collections.Counter(response.json()["status"] for response in responses)
        assert statuses == {"REQUESTED": 3, "NO_DRIVER_FOUND": 5}
        assert await pending_offer_drivers(db) == sorted(who["driver"].id for who in drivers)

        # Free the drivers for the next round.
        for who, response in zip(riders, responses):
            if response.json()["status"] == "REQUESTED":
                cancelled = await client.post(f"/rides/{response.json()['id']}/cancel", headers=who["headers"])
                assert cancelled.status_code == 200


async def test_one_rider_sending_four_requests_gets_one_ride(client, db, rider, online_at, monkeypatch):
    await online_at(500)
    widen(monkeypatch, rides_repo, "get_active_for_rider", 0.05)

    responses = await asyncio.gather(*[post_ride(client, rider) for _ in range(4)])

    assert sorted(response.status_code for response in responses) == [201, 409, 409, 409]
    active = await db.scalar(
        select(func.count()).select_from(Ride).where(Ride.rider_id == rider["user"].id, Ride.status.in_(ACTIVE_RIDE_STATUSES))
    )
    assert active == 1


async def test_two_offers_of_one_driver_accepted_at_once_assign_one_ride(
    client, db, driver, make_user, insert_ride, put_online, monkeypatch
):
    await put_online(driver, RIDE_BODY["pickup_lat"], RIDE_BODY["pickup_lng"])
    rides = [await insert_ride(await make_user("rider"), RideStatus.REQUESTED) for _ in range(2)]
    offer_ids = []
    for ride in rides:
        offer = RideOffer(
            ride_id=ride.id, driver_id=driver["driver"].id, status=OfferStatus.PENDING, pickup_distance_m=100,
            expires_at=datetime.now(timezone.utc) + timedelta(seconds=60),
        )
        db.add(offer)
        await db.flush()
        offer_ids.append(offer.id)
    await db.commit()
    widen(monkeypatch, rides_repo, "get_active_for_driver", 0.05)

    responses = await asyncio.gather(*[client.post(f"/offers/{offer_id}/accept", headers=driver["headers"]) for offer_id in offer_ids])

    assert sorted(response.status_code for response in responses) == [200, 409]
    active = await db.scalar(
        select(func.count()).select_from(Ride).where(Ride.driver_id == driver["driver"].id, Ride.status.in_(ACTIVE_RIDE_STATUSES))
    )
    assert active == 1
    loser = rides[0] if responses[0].status_code == 409 else rides[1]
    assert await db.scalar(select(Ride.status).where(Ride.id == loser.id)) == RideStatus.REQUESTED


async def test_a_driver_held_by_someone_else_is_skipped_not_failed(client, db, make_user, online_at, hold_row):
    nearest, second = await online_at(300), await online_at(1200)
    rider_1, rider_2 = await make_user("rider"), await make_user("rider")
    holder = await hold_row("drivers", nearest["driver"].id)

    ride = (await post_ride(client, rider_1)).json()

    assert ride["status"] == "REQUESTED"
    assert await offers_of(db, ride["id"]) == [(second["driver"].id, "PENDING")]

    await holder.rollback()
    ride_2 = (await post_ride(client, rider_2)).json()

    assert await offers_of(db, ride_2["id"]) == [(nearest["driver"].id, "PENDING")]


async def test_no_lock_is_left_behind(client, db, make_user, online_at, test_engine, monkeypatch):
    driver = await online_at(500)
    rider_1, rider_2, rider_3 = [await make_user("rider") for _ in range(3)]
    driver_ids = [driver["driver"].id]
    user_ids = [driver["user"].id, rider_1["user"].id, rider_2["user"].id, rider_3["user"].id]

    # A request that fails halfway: the offer insert raises.
    real_create = offers_repo.create

    async def broken(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(offers_repo, "create", broken)
    with pytest.raises(RuntimeError):
        await post_ride(client, rider_1)
    monkeypatch.setattr(offers_repo, "create", real_create)
    await assert_no_lock_left(test_engine, driver_ids, user_ids)

    # The next request works, and leaves nothing either.
    assert (await post_ride(client, rider_2)).json()["status"] == "REQUESTED"
    await assert_no_lock_left(test_engine, driver_ids, user_ids)

    # The driver is busy now: this one ends as NO_DRIVER_FOUND.
    assert (await post_ride(client, rider_3)).json()["status"] == "NO_DRIVER_FOUND"
    await assert_no_lock_left(test_engine, driver_ids, user_ids)


async def test_a_row_that_stays_locked_gives_503_after_the_wait(client, db, make_user, online_at, hold_row, monkeypatch):
    monkeypatch.setattr(database, "LOCK_WAIT_MS", 300)
    driver = await online_at(500)
    rider_1, rider_2 = await make_user("rider"), await make_user("rider")
    ride = (await post_ride(client, rider_1)).json()
    offer_id = (await db.scalar(select(RideOffer.id).where(RideOffer.ride_id == ride["id"])))

    # Accept: another session keeps the driver row locked.
    holder = await hold_row("drivers", driver["driver"].id)
    started = time.monotonic()
    # The limit turns "waits forever" (no lock timeout) into a failing test instead of a hanging one.
    response = await asyncio.wait_for(client.post(f"/offers/{offer_id}/accept", headers=driver["headers"]), 5)
    waited = time.monotonic() - started
    await holder.rollback()

    assert response.status_code == 503
    assert response.json() == {"detail": "Busy, please retry"}
    assert 0.25 < waited < 2
    assert await db.scalar(select(RideOffer.status).where(RideOffer.id == offer_id)) == OfferStatus.PENDING
    assert await db.scalar(select(Ride.status).where(Ride.id == ride["id"])) == RideStatus.REQUESTED

    # Request a ride: another session keeps the rider's user row locked.
    holder = await hold_row("users", rider_2["user"].id)
    started = time.monotonic()
    response = await asyncio.wait_for(post_ride(client, rider_2), 5)
    waited = time.monotonic() - started
    await holder.rollback()

    assert response.status_code == 503
    assert response.json() == {"detail": "Busy, please retry"}
    assert 0.25 < waited < 2
    assert await db.scalar(select(func.count()).select_from(Ride).where(Ride.rider_id == rider_2["user"].id)) == 0
