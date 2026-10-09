"""M4.3: edge cases around offers, and the unique indexes that stay true when a lock is missed."""
import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError

from app.database import redis_client
from app.models import ACTIVE_RIDE_STATUSES, OfferStatus, Ride, RideOffer, RideStatus
from app.repositories import drivers as drivers_repo
from app.repositories import offers as offers_repo
from app.repositories import rides as rides_repo
from app.services import offers as offers_service
from conftest import widen
from test_matching import offers_of, online_at, request_ride  # noqa: F401  (the last two are fixtures)
from test_offers import make_overdue, offer_of, ride_events, ride_status
from test_rides import RIDE_BODY
from test_websocket import assert_silent, open_socket, receive  # noqa: F401  (open_socket is a fixture)

ROUNDS = 10


@pytest.fixture(autouse=True)
def forget_failed_offers():
    """Offer ids restart at 1 in every test, so the sweeper's memory of failed offers must not carry over."""
    offers_service.failed_offer_ids.clear()


@pytest.fixture
def logged(caplog):
    """The app logs through uvicorn's logger, which uvicorn may stop from reaching the root logger: attach caplog directly."""
    logger = logging.getLogger("uvicorn.error")
    propagated = logger.propagate
    logger.propagate = False  # otherwise a record could reach caplog twice
    logger.addHandler(caplog.handler)
    caplog.set_level(logging.INFO, logger="uvicorn.error")
    yield caplog
    logger.removeHandler(caplog.handler)
    logger.propagate = propagated


async def drop_presence(who: dict) -> None:
    """What the driver going offline, an admin rejection, and a page that died all end up as: no presence key."""
    await redis_client.delete(f"driver:{who['driver'].id}:presence")


async def race(**calls) -> dict:
    """Starts all the calls at the same moment (in the given order) and returns their results by name."""
    results = await asyncio.gather(*calls.values())
    return dict(zip(calls, results))


async def offer_id_of(db, ride_id: int) -> int:
    return await db.scalar(select(RideOffer.id).where(RideOffer.ride_id == ride_id).order_by(RideOffer.id.desc()))


async def pending_count(db) -> int:
    return await db.scalar(select(func.count()).select_from(RideOffer).where(RideOffer.status == OfferStatus.PENDING))


def accept_call(client, who: dict, offer_id: int):
    return client.post(f"/offers/{offer_id}/accept", headers=who["headers"])


def cancel_call(client, who: dict, ride_id: int):
    return client.post(f"/rides/{ride_id}/cancel", headers=who["headers"])


# --- A. a driver who disappears while holding an offer ---


async def test_a_driver_who_vanishes_loses_the_offer_and_the_next_driver_gets_it(
    client, db, open_socket, rider, online_at, request_ride
):
    first, second = await online_at(500), await online_at(1200)
    first_ws, second_ws, rider_ws = await open_socket(first), await open_socket(second), await open_socket(rider)
    ride = await request_ride(rider)
    first_offer = await offer_of(client, first)
    await receive(first_ws)
    await drop_presence(first)

    await offers_service.expire_due_offers()

    assert await offers_of(db, ride["id"]) == [(first["driver"].id, "EXPIRED"), (second["driver"].id, "PENDING")]
    assert await db.scalar(select(RideOffer.responded_at).where(RideOffer.id == first_offer["id"])) is None
    assert await receive(first_ws) == {
        "type": "offer_closed",
        "data": {"offer_id": first_offer["id"], "ride_id": ride["id"], "reason": "driver_offline"},
    }
    second_offer = await offer_of(client, second)
    assert await receive(second_ws) == {"type": "offer_created", "data": {"offer_id": second_offer["id"], "ride_id": ride["id"]}}
    assert await ride_status(db, ride["id"]) == "REQUESTED"
    assert await ride_events(db, ride["id"]) == [(None, "REQUESTED", rider["user"].id)]  # no new event
    await assert_silent(rider_ws)


async def test_a_driver_who_vanishes_with_nobody_else_ends_the_ride(client, db, open_socket, rider, online_at, request_ride):
    driver = await online_at(500)
    rider_ws = await open_socket(rider)
    ride = await request_ride(rider)
    await drop_presence(driver)

    await offers_service.expire_due_offers()

    assert await offers_of(db, ride["id"]) == [(driver["driver"].id, "EXPIRED")]
    assert await ride_events(db, ride["id"]) == [(None, "REQUESTED", rider["user"].id), ("REQUESTED", "NO_DRIVER_FOUND", None)]
    assert await receive(rider_ws) == {"type": "ride_updated", "data": {"ride_id": ride["id"], "status": "NO_DRIVER_FOUND"}}


@pytest.mark.parametrize("cause", ["goes_offline", "admin_rejects"])
async def test_the_real_causes_of_a_vanished_driver(client, db, open_socket, admin, rider, online_at, request_ride, cause):
    first, second = await online_at(500), await online_at(1200)
    first_ws = await open_socket(first)
    ride = await request_ride(rider)
    offer = await offer_of(client, first)
    await receive(first_ws)

    if cause == "goes_offline":
        response = await client.post("/drivers/me/offline", headers=first["headers"])
    else:
        response = await client.post(f"/admin/drivers/{first['driver'].id}/reject", headers=admin["headers"])
    assert response.status_code == 200
    assert await offers_of(db, ride["id"]) == [(first["driver"].id, "PENDING")]  # nothing happens until the sweep

    await offers_service.expire_due_offers()

    assert await offers_of(db, ride["id"]) == [(first["driver"].id, "EXPIRED"), (second["driver"].id, "PENDING")]
    assert await receive(first_ws) == {
        "type": "offer_closed",
        "data": {"offer_id": offer["id"], "ride_id": ride["id"], "reason": "driver_offline"},
    }


async def test_a_sweep_leaves_the_offer_of_an_online_driver_alone(client, db, open_socket, rider, online_at, request_ride):
    driver = await online_at(500)
    driver_ws = await open_socket(driver)
    ride = await request_ride(rider)
    await receive(driver_ws)

    await offers_service.expire_due_offers()

    assert await offers_of(db, ride["id"]) == [(driver["driver"].id, "PENDING")]
    await assert_silent(driver_ws)


async def test_time_wins_when_the_offer_is_overdue_and_the_driver_is_gone(client, db, open_socket, rider, online_at, request_ride):
    driver = await online_at(500)
    driver_ws = await open_socket(driver)
    ride = await request_ride(rider)
    offer = await offer_of(client, driver)
    await receive(driver_ws)
    await make_overdue(db, ride["id"])
    await drop_presence(driver)

    await offers_service.expire_due_offers()

    assert await receive(driver_ws) == {
        "type": "offer_closed",
        "data": {"offer_id": offer["id"], "ride_id": ride["id"], "reason": "expired"},
    }


async def test_a_driver_who_is_gone_cannot_accept_before_or_after_the_sweep(client, db, rider, online_at, request_ride):
    driver = await online_at(500)
    ride = await request_ride(rider)
    offer = await offer_of(client, driver)
    await drop_presence(driver)

    before = await accept_call(client, driver, offer["id"])
    await offers_service.expire_due_offers()
    after = await accept_call(client, driver, offer["id"])

    assert before.status_code == 409
    assert "offline" in before.json()["detail"]
    assert after.status_code == 409
    assert after.json()["detail"] == "This offer is no longer available"
    assert await ride_status(db, ride["id"]) == "NO_DRIVER_FOUND"


async def test_the_real_sweeper_withdraws_the_offer_of_a_vanished_driver_on_its_own(live_server, db, rider, online_at, request_ride):
    first, second = await online_at(500), await online_at(1200)
    ride = await request_ride(rider)
    await drop_presence(first)

    # No other call is made: only the background task can do this.
    started = time.monotonic()
    while await offers_of(db, ride["id"]) != [(first["driver"].id, "EXPIRED"), (second["driver"].id, "PENDING")]:
        assert time.monotonic() - started < 3.5, "the sweeper did not withdraw the offer"
        await asyncio.sleep(0.1)


# --- B. cancel, expiry, reject, and accept racing each other ---


async def test_accept_racing_a_cancel_always_ends_cancelled(client, db, make_user, online_at, request_ride):
    driver = await online_at(500)
    rider = await make_user("rider")
    patterns = set()

    for round_number in range(ROUNDS):
        ride = await request_ride(rider)
        offer_id = await offer_id_of(db, ride["id"])
        accept, cancel = accept_call(client, driver, offer_id), cancel_call(client, rider, ride["id"])
        results = await (race(accept=accept, cancel=cancel) if round_number % 2 == 0 else race(cancel=cancel, accept=accept))

        assert results["cancel"].status_code == 200
        assert await ride_status(db, ride["id"]) == "CANCELLED"
        assert await pending_count(db) == 0
        driver_id = await db.scalar(select(Ride.driver_id).where(Ride.id == ride["id"]))
        offer_status = (await offers_of(db, ride["id"]))[0][1]
        events = [to for _, to, _ in await ride_events(db, ride["id"])]
        if results["accept"].status_code == 409:
            assert (offer_status, driver_id, events) == ("CANCELLED", None, ["REQUESTED", "CANCELLED"])
        else:
            assert results["accept"].status_code == 200
            assert (offer_status, driver_id, events) == ("ACCEPTED", driver["driver"].id, ["REQUESTED", "DRIVER_ASSIGNED", "CANCELLED"])
        patterns.add(offer_status)

        # The driver is matchable again: free of rides and offers.
        assert await drivers_repo.get_available_ids(db, [driver["driver"].id]) == {driver["driver"].id}
    assert patterns <= {"CANCELLED", "ACCEPTED"}


async def test_a_sweep_racing_an_accept_of_an_overdue_offer(client, db, make_user, online_at, request_ride):
    driver = await online_at(500)
    rider = await make_user("rider")

    for round_number in range(ROUNDS):
        ride = await request_ride(rider)
        offer_id = await offer_id_of(db, ride["id"])
        await make_overdue(db, ride["id"])
        accept, sweep = accept_call(client, driver, offer_id), offers_service.expire_due_offers()
        results = await (race(accept=accept, sweep=sweep) if round_number % 2 == 0 else race(sweep=sweep, accept=accept))

        assert results["accept"].status_code == 409
        assert await offers_of(db, ride["id"]) == [(driver["driver"].id, "EXPIRED")]  # handled once
        # One ride event for the end of the ride, nothing doubled.
        assert [to for _, to, _ in await ride_events(db, ride["id"])] == ["REQUESTED", "NO_DRIVER_FOUND"]


async def test_a_sweep_racing_an_accept_of_a_live_offer_does_nothing_and_the_accept_wins(client, db, make_user, online_at, request_ride):
    driver = await online_at(500)
    rider = await make_user("rider")

    for round_number in range(ROUNDS):
        ride = await request_ride(rider)
        offer_id = await offer_id_of(db, ride["id"])
        accept, sweep = accept_call(client, driver, offer_id), offers_service.expire_due_offers()
        results = await (race(accept=accept, sweep=sweep) if round_number % 2 == 0 else race(sweep=sweep, accept=accept))

        assert results["accept"].status_code == 200
        assert await offers_of(db, ride["id"]) == [(driver["driver"].id, "ACCEPTED")]
        assert await ride_status(db, ride["id"]) == "DRIVER_ASSIGNED"
        assert (await cancel_call(client, rider, ride["id"])).status_code == 200  # frees the driver for the next round


@pytest.mark.parametrize("next_driver_exists", [True, False])
async def test_two_sweeps_at_once_handle_an_overdue_offer_once(client, db, open_socket, rider, online_at, request_ride, next_driver_exists):
    first = await online_at(500)
    second = await online_at(1200) if next_driver_exists else None
    first_ws = await open_socket(first)
    ride = await request_ride(rider)
    await receive(first_ws)
    await make_overdue(db, ride["id"])

    await asyncio.gather(offers_service.expire_due_offers(), offers_service.expire_due_offers())

    if next_driver_exists:
        assert await offers_of(db, ride["id"]) == [(first["driver"].id, "EXPIRED"), (second["driver"].id, "PENDING")]
        assert [to for _, to, _ in await ride_events(db, ride["id"])] == ["REQUESTED"]
    else:
        assert await offers_of(db, ride["id"]) == [(first["driver"].id, "EXPIRED")]
        assert [to for _, to, _ in await ride_events(db, ride["id"])] == ["REQUESTED", "NO_DRIVER_FOUND"]
    assert (await receive(first_ws))["data"]["reason"] == "expired"
    await assert_silent(first_ws)  # exactly one offer_closed


async def test_a_reject_racing_a_sweep_of_an_overdue_offer(client, db, make_user, online_at, request_ride):
    first, second = await online_at(500), await online_at(1200)
    rider = await make_user("rider")

    for round_number in range(ROUNDS):
        ride = await request_ride(rider)
        offer_id = await offer_id_of(db, ride["id"])
        await make_overdue(db, ride["id"])
        reject = client.post(f"/offers/{offer_id}/reject", headers=first["headers"])
        sweep = offers_service.expire_due_offers()
        results = await (race(reject=reject, sweep=sweep) if round_number % 2 == 0 else race(sweep=sweep, reject=reject))

        # The deadline is hard: the reject never wins, the sweep processes the offer, and exactly one next offer exists.
        assert results["reject"].status_code == 409
        assert await offers_of(db, ride["id"]) == [(first["driver"].id, "EXPIRED"), (second["driver"].id, "PENDING")]
        assert (await cancel_call(client, rider, ride["id"])).status_code == 200


async def test_a_reject_racing_the_withdrawal_of_a_gone_driver_processes_the_offer_once(client, db, make_user, online_at, request_ride):
    first, second = await online_at(500), await online_at(1200)
    rider = await make_user("rider")

    for round_number in range(ROUNDS):
        await online_at_again(client, first)
        ride = await request_ride(rider)
        offer_id = await offer_id_of(db, ride["id"])
        await drop_presence(first)
        reject = client.post(f"/offers/{offer_id}/reject", headers=first["headers"])
        sweep = offers_service.expire_due_offers()
        results = await (race(reject=reject, sweep=sweep) if round_number % 2 == 0 else race(sweep=sweep, reject=reject))

        # Either the reject or the sweep closes it, never both, and the ride is offered onward exactly once.
        first_status = "REJECTED" if results["reject"].status_code == 204 else "EXPIRED"
        assert results["reject"].status_code in (204, 409)
        assert await offers_of(db, ride["id"]) == [(first["driver"].id, first_status), (second["driver"].id, "PENDING")]
        assert (await cancel_call(client, rider, ride["id"])).status_code == 200


async def online_at_again(client, who: dict) -> None:
    response = await client.post("/drivers/me/online", json={"lat": RIDE_BODY["pickup_lat"], "lng": RIDE_BODY["pickup_lng"]}, headers=who["headers"])
    assert response.status_code == 200


async def test_a_cancel_racing_a_sweep_leaves_a_cancelled_ride_and_no_open_offer(client, db, make_user, online_at, request_ride):
    await online_at(500)
    await online_at(1200)
    rider = await make_user("rider")

    for round_number in range(ROUNDS):
        ride = await request_ride(rider)
        await make_overdue(db, ride["id"])
        cancel, sweep = cancel_call(client, rider, ride["id"]), offers_service.expire_due_offers()
        results = await (race(cancel=cancel, sweep=sweep) if round_number % 2 == 0 else race(sweep=sweep, cancel=cancel))

        assert results["cancel"].status_code == 200
        assert await ride_status(db, ride["id"]) == "CANCELLED"
        assert await pending_count(db) == 0


# --- C. duplicate accepts and accepts after the deadline ---


async def test_five_accepts_of_one_offer_at_once_give_one_winner(client, db, open_socket, rider, online_at, request_ride):
    driver = await online_at(500)
    rider_ws = await open_socket(rider)
    ride = await request_ride(rider)
    offer = await offer_of(client, driver)

    responses = await asyncio.gather(*[accept_call(client, driver, offer["id"]) for _ in range(5)])

    assert sorted(response.status_code for response in responses) == [200, 409, 409, 409, 409]
    assert [to for _, to, _ in await ride_events(db, ride["id"])] == ["REQUESTED", "DRIVER_ASSIGNED"]
    assert await receive(rider_ws) == {"type": "ride_updated", "data": {"ride_id": ride["id"], "status": "DRIVER_ASSIGNED"}}
    await assert_silent(rider_ws)  # one ride_updated, not five
    assert (await accept_call(client, driver, offer["id"])).status_code == 409


@pytest.mark.parametrize("action", ["accept", "reject"])
async def test_answering_one_millisecond_after_the_deadline_is_409_and_changes_nothing(client, db, rider, online_at, request_ride, action):
    driver = await online_at(500)
    ride = await request_ride(rider)
    offer = await offer_of(client, driver)
    await db.execute(update(RideOffer).where(RideOffer.id == offer["id"]).values(expires_at=datetime.now(timezone.utc) - timedelta(milliseconds=1)))
    await db.commit()

    response = await client.post(f"/offers/{offer['id']}/{action}", headers=driver["headers"])

    assert response.status_code == 409
    assert response.json()["detail"] == "This offer has expired"
    assert await offers_of(db, ride["id"]) == [(driver["driver"].id, "PENDING")]
    assert await ride_events(db, ride["id"]) == [(None, "REQUESTED", rider["user"].id)]

    await offers_service.expire_due_offers()  # the sweeper still handles the ride

    assert await offers_of(db, ride["id"]) == [(driver["driver"].id, "EXPIRED")]
    assert await ride_status(db, ride["id"]) == "NO_DRIVER_FOUND"


async def test_two_drivers_accepting_two_different_offers_at_once_both_win(client, db, make_user, online_at, request_ride):
    first, second = await online_at(500), await online_at(1200)
    rides = [await request_ride(await make_user("rider")) for _ in range(2)]
    offers = [(await offer_of(client, first))["id"], (await offer_of(client, second))["id"]]

    responses = await asyncio.gather(accept_call(client, first, offers[0]), accept_call(client, second, offers[1]))

    assert [response.status_code for response in responses] == [200, 200]
    assert [await ride_status(db, ride["id"]) for ride in rides] == ["DRIVER_ASSIGNED", "DRIVER_ASSIGNED"]


# --- D. the unique indexes alone (these tests never rely on a lock) ---


async def expect_violation(db, index: str, action) -> None:
    with pytest.raises(IntegrityError, match=index):
        await action()
    await db.rollback()


def new_offer(ride_id: int, driver_id: int, status: OfferStatus) -> RideOffer:
    return RideOffer(
        ride_id=ride_id, driver_id=driver_id, status=status, pickup_distance_m=100,
        expires_at=datetime.now(timezone.utc) + timedelta(seconds=15),
    )


async def test_the_indexes_refuse_what_the_locks_prevent(db, locks_disabled, make_user, insert_ride):
    driver, rider_1, rider_2 = await make_user("driver"), await make_user("rider"), await make_user("rider")
    driver_id, rider_1_id = driver["driver"].id, rider_1["user"].id  # ids first: a rollback expires everything the session holds
    ride_1 = (await insert_ride(rider_1, RideStatus.REQUESTED)).id
    ride_2 = (await insert_ride(rider_2, RideStatus.REQUESTED)).id
    db.add(new_offer(ride_1, driver_id, OfferStatus.PENDING))
    await db.commit()

    async def second_pending_offer():
        db.add(new_offer(ride_2, driver_id, OfferStatus.PENDING))
        await db.commit()

    await expect_violation(db, "uq_ride_offers_one_pending_per_driver", second_pending_offer)

    # A second active ride for one driver.
    await db.execute(update(Ride).where(Ride.id == ride_1).values(status=RideStatus.DRIVER_ASSIGNED, driver_id=driver_id))
    await db.commit()

    async def second_active_ride_for_the_driver():
        await db.execute(update(Ride).where(Ride.id == ride_2).values(status=RideStatus.DRIVER_ARRIVED, driver_id=driver_id))
        await db.commit()

    await expect_violation(db, "uq_rides_one_active_per_driver", second_active_ride_for_the_driver)

    # A second active ride for one rider.
    async def second_active_ride_for_the_rider():
        await db.execute(update(Ride).where(Ride.id == ride_2).values(rider_id=rider_1_id))
        await db.commit()

    await expect_violation(db, "uq_rides_one_active_per_rider", second_active_ride_for_the_rider)
    assert locks_disabled == {}  # the SQL above never went through a lock function


@pytest.mark.parametrize("finished", [OfferStatus.EXPIRED, OfferStatus.REJECTED, OfferStatus.CANCELLED, OfferStatus.ACCEPTED])
async def test_a_new_pending_offer_is_fine_after_the_previous_one_is_finished(db, locks_disabled, make_user, insert_ride, finished):
    driver, rider_1, rider_2 = await make_user("driver"), await make_user("rider"), await make_user("rider")
    ride_1, ride_2 = (await insert_ride(rider_1, RideStatus.REQUESTED)).id, (await insert_ride(rider_2, RideStatus.REQUESTED)).id
    db.add(new_offer(ride_1, driver["driver"].id, finished))
    db.add(new_offer(ride_2, driver["driver"].id, OfferStatus.PENDING))

    await db.commit()


@pytest.mark.parametrize("finished", [RideStatus.COMPLETED, RideStatus.CANCELLED, RideStatus.NO_DRIVER_FOUND])
async def test_a_new_active_ride_is_fine_after_the_previous_one_is_finished(db, locks_disabled, make_user, insert_ride, finished):
    driver, rider = await make_user("driver"), await make_user("rider")
    await insert_ride(rider, finished, driver if finished != RideStatus.NO_DRIVER_FOUND else None)

    await insert_ride(rider, RideStatus.DRIVER_ASSIGNED, driver)  # same rider, same driver
    await insert_ride(await make_user("rider"), RideStatus.REQUESTED)  # nothing else is in the way


async def test_two_finished_rides_or_two_requests_of_different_riders_are_fine(db, locks_disabled, make_user, insert_ride):
    rider = await make_user("rider")
    await insert_ride(rider, RideStatus.NO_DRIVER_FOUND)
    await insert_ride(rider, RideStatus.NO_DRIVER_FOUND)
    await insert_ride(await make_user("rider"), RideStatus.REQUESTED)
    await insert_ride(await make_user("rider"), RideStatus.REQUESTED)


async def test_without_locks_two_riders_and_one_driver_still_give_one_offer(client, db, logged, locks_disabled, make_user, online_at, monkeypatch):
    driver = await online_at(500)
    riders = [await make_user("rider") for _ in range(2)]
    widen(monkeypatch, drivers_repo, "get_available_ids", 0.05)  # both requests are told "free" before either commits

    responses = await asyncio.gather(*[client.post("/rides", json=RIDE_BODY, headers=who["headers"]) for who in riders])

    assert [response.status_code for response in responses] == [201, 201]
    assert sorted(response.json()["status"] for response in responses) == ["NO_DRIVER_FOUND", "REQUESTED"]
    assert await db.scalar(select(func.count()).select_from(RideOffer)) == 1
    assert (await db.scalar(select(RideOffer.driver_id))) == driver["driver"].id
    assert locks_disabled["drivers.try_lock"] >= 2  # the requests did go through the place where the lock would be
    skipped = [record.getMessage() for record in logged.records if "offer skipped for driver" in record.getMessage()]
    assert len(skipped) == 1
    assert skipped[0].startswith(f"offer skipped for driver {driver['driver'].id}: ")
    assert "uq_ride_offers_one_pending_per_driver" in skipped[0]


async def test_without_locks_eight_riders_and_three_drivers_still_give_three_offers(client, db, locks_disabled, make_user, online_at, monkeypatch):
    drivers = [await online_at(meters) for meters in (300, 900, 1500)]
    riders = [await make_user("rider") for _ in range(8)]
    widen(monkeypatch, drivers_repo, "get_available_ids", 0.02)

    responses = await asyncio.gather(*[client.post("/rides", json=RIDE_BODY, headers=who["headers"]) for who in riders])

    assert [response.status_code for response in responses] == [201] * 8
    assert sorted(response.json()["status"] for response in responses) == ["NO_DRIVER_FOUND"] * 5 + ["REQUESTED"] * 3
    offered = (await db.execute(select(RideOffer.driver_id).where(RideOffer.status == OfferStatus.PENDING))).scalars().all()
    assert sorted(offered) == sorted(who["driver"].id for who in drivers)


async def test_without_locks_four_requests_of_one_rider_still_give_one_ride(client, db, locks_disabled, rider, online_at, monkeypatch):
    await online_at(500)  # a driver is online, so the first ride stays REQUESTED (active) and the others must be refused
    widen(monkeypatch, rides_repo, "get_active_for_rider", 0.05)

    responses = await asyncio.gather(*[client.post("/rides", json=RIDE_BODY, headers=rider["headers"]) for _ in range(4)])

    assert sorted(response.status_code for response in responses) == [201, 409, 409, 409]
    assert {response.json()["detail"] for response in responses if response.status_code == 409} == {"You already have an active ride"}
    active = await db.scalar(
        select(func.count()).select_from(Ride).where(Ride.rider_id == rider["user"].id, Ride.status.in_(ACTIVE_RIDE_STATUSES))
    )
    assert active == 1
    assert await db.scalar(select(func.count()).select_from(Ride)) == 1  # the refused requests left no rows behind


async def test_accept_turns_an_index_violation_into_a_409_and_undoes_everything(client, db, make_user, online_at, insert_ride, monkeypatch):
    driver = await online_at(500)
    busy_ride = await insert_ride(await make_user("rider"), RideStatus.DRIVER_ASSIGNED, driver)
    other_ride = await insert_ride(await make_user("rider"), RideStatus.REQUESTED)
    offer = new_offer(other_ride.id, driver["driver"].id, OfferStatus.PENDING)  # the indexes allow this: no second PENDING offer
    db.add(offer)
    await db.commit()
    offer_id = offer.id

    # The normal check refuses first.
    normal = await accept_call(client, driver, offer_id)
    assert (normal.status_code, normal.json()["detail"]) == (409, "You already have an active ride")

    # A stale check (it finds nothing) lets the accept reach the database, and the index refuses it there.
    async def stale(db, driver_id):
        return None

    monkeypatch.setattr(rides_repo, "get_active_for_driver", stale)
    response = await accept_call(client, driver, offer_id)

    assert (response.status_code, response.json()["detail"]) == (409, "You already have an active ride")
    assert await ride_status(db, other_ride.id) == "REQUESTED"
    assert await ride_events(db, other_ride.id) == []
    assert await db.scalar(select(RideOffer.status).where(RideOffer.id == offer_id)) == OfferStatus.PENDING
    assert await ride_status(db, busy_ride.id) == "DRIVER_ASSIGNED"


async def test_a_failed_offer_insert_skips_that_driver_and_the_request_goes_on(client, db, logged, rider, online_at, monkeypatch):
    nearest, second = await online_at(300), await online_at(900)
    real_create = offers_repo.create
    calls = []

    async def create(db, ride_id, driver_id, *args):
        calls.append(driver_id)
        if len(calls) == 1:
            raise IntegrityError("INSERT INTO ride_offers", {}, Exception("uq_ride_offers_one_pending_per_driver"))
        return await real_create(db, ride_id, driver_id, *args)

    monkeypatch.setattr(offers_repo, "create", create)

    response = await client.post("/rides", json=RIDE_BODY, headers=rider["headers"])

    assert response.status_code == 201
    assert response.json()["status"] == "REQUESTED"
    assert calls == [nearest["driver"].id, second["driver"].id]
    assert await offers_of(db, response.json()["id"]) == [(second["driver"].id, "PENDING")]
    assert any(f"offer skipped for driver {nearest['driver'].id}" in record.getMessage() for record in logged.records)


# --- E. the sweeper does not let one offer block the others ---


async def three_overdue_offers(client, db, make_user, online_at, request_ride) -> list[tuple[dict, dict]]:
    """Three drivers, three riders, three overdue offers (one each), oldest first. Returns (rider, ride) pairs."""
    for meters in (300, 900, 1500):
        await online_at(meters)
    pairs = []
    for _ in range(3):
        rider = await make_user("rider")
        pairs.append((rider, await request_ride(rider)))
    for _, ride in pairs:
        await make_overdue(db, ride["id"])
    return pairs


async def test_one_failing_offer_does_not_block_the_others_and_is_logged_once(client, db, logged, make_user, online_at, request_ride, monkeypatch):
    pairs = await three_overdue_offers(client, db, make_user, online_at, request_ride)
    rides = [ride for _, ride in pairs]
    failing_offer_id = await offer_id_of(db, rides[0]["id"])
    real_finish = offers_service.finish_offer

    async def finish(db, ride, offer, new_status, reason):
        if offer.id == failing_offer_id:
            raise ValueError("this one is broken")
        await real_finish(db, ride, offer, new_status, reason)

    monkeypatch.setattr(offers_service, "finish_offer", finish)

    for _ in range(3):
        await offers_service.expire_due_offers()  # never raises

    # The other two were processed in the same pass: the second ride has no driver left, and the third was offered onward
    # (the second driver's offer had just been closed, so that driver was free again).
    assert await ride_status(db, rides[1]["id"]) == "NO_DRIVER_FOUND"
    assert [status for _, status in await offers_of(db, rides[2]["id"])] == ["EXPIRED", "PENDING"]
    assert await db.scalar(select(RideOffer.status).where(RideOffer.id == failing_offer_id)) == OfferStatus.PENDING
    warnings = [record.getMessage() for record in logged.records if "could not handle" in record.getMessage()]
    assert len(warnings) == 1
    assert f"offer {failing_offer_id}" in warnings[0]
    assert offers_service.failed_offer_ids == {failing_offer_id}

    # Once the offer is not pending any more it is forgotten.
    assert (await cancel_call(client, pairs[0][0], rides[0]["id"])).status_code == 200
    await offers_service.expire_due_offers()
    assert offers_service.failed_offer_ids == set()


async def test_a_redis_error_aborts_the_pass(client, db, make_user, online_at, request_ride, monkeypatch):
    rides = [ride for _, ride in await three_overdue_offers(client, db, make_user, online_at, request_ride)]

    async def broken(driver_ids):
        raise RedisConnectionError("redis is down")

    monkeypatch.setattr(drivers_repo, "get_online_ids", broken)
    with pytest.raises(RedisConnectionError):
        await offers_service.expire_due_offers()
    assert [await ride_status(db, ride["id"]) for ride in rides] == ["REQUESTED"] * 3


async def test_a_redis_error_while_handling_one_offer_aborts_the_pass_too(client, db, logged, make_user, online_at, request_ride, monkeypatch):
    await three_overdue_offers(client, db, make_user, online_at, request_ride)

    async def finish(*args):
        raise RedisConnectionError("redis went away")

    monkeypatch.setattr(offers_service, "finish_offer", finish)
    with pytest.raises(RedisConnectionError):
        await offers_service.expire_due_offers()
    assert not any("could not handle" in record.getMessage() for record in logged.records)


async def test_the_presence_lookup_is_never_called_with_an_empty_list(client, monkeypatch):
    async def forbidden(driver_ids):
        raise AssertionError("MGET with no keys is an error")

    monkeypatch.setattr(drivers_repo, "get_online_ids", forbidden)

    await offers_service.expire_due_offers()  # nothing is pending


async def test_one_presence_lookup_serves_all_pending_offers(client, make_user, online_at, request_ride, monkeypatch):
    drivers = [await online_at(meters) for meters in (300, 900)]
    for _ in range(2):
        await request_ride(await make_user("rider"))
    real = drivers_repo.get_online_ids
    calls = []

    async def spy(driver_ids):
        calls.append(sorted(driver_ids))
        return await real(driver_ids)

    monkeypatch.setattr(drivers_repo, "get_online_ids", spy)

    await offers_service.expire_due_offers()

    assert calls == [sorted(who["driver"].id for who in drivers)]
