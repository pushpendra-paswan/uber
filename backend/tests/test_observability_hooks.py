import asyncio

import pytest
from prometheus_client import REGISTRY
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.models import RideEvent, RideStatus
from app.observability import hooks, middleware
from app.observability.hooks import start_background_task
from app.repositories import rides as rides_repo
from app.services import offers as offers_service
from app.services import payments
from test_logging import log_lines  # noqa: F401  (a fixture)
from test_offers import make_overdue
from test_rides import RIDE_BODY
from test_trip import advance, cancel

FULL_TRIP = [
    ("none", "REQUESTED"), ("REQUESTED", "DRIVER_ASSIGNED"), ("DRIVER_ASSIGNED", "DRIVER_ARRIVED"),
    ("DRIVER_ARRIVED", "IN_PROGRESS"), ("IN_PROGRESS", "COMPLETED"),
]


def counter(from_status: str, to_status: str) -> float:
    return REGISTRY.get_sample_value("ride_transitions_total", {"from_status": from_status, "to_status": to_status}) or 0


def hook_errors() -> float:
    return REGISTRY.get_sample_value("observability_errors_total", {"source": "hook"}) or 0


def transitions(lines: list[dict], ride_id: int | None = None) -> list[tuple]:
    return [
        (line["from_status"], line["to_status"]) for line in lines
        if line["msg"] == "ride_transition" and (ride_id is None or line["ride_id"] == ride_id)
    ]


async def events_in_sql(db, ride_id: int) -> list[tuple]:
    """Written independently of the application code: the rows of ride_events, in order."""
    result = await db.execute(select(RideEvent.from_status, RideEvent.to_status).where(RideEvent.ride_id == ride_id).order_by(RideEvent.id))
    return [(from_status.value if from_status else "none", to_status.value) for from_status, to_status in result.all()]


# --- after the commit ---


async def test_a_whole_trip_emits_five_transitions_in_order_and_they_match_the_database(client, db, log_lines, rider, driver, assign_ride):
    before = {pair: counter(*pair) for pair in FULL_TRIP}

    ride = await assign_ride(rider, driver)
    await advance(client, {"rider": rider, "driver": driver, "id": ride["id"]}, "COMPLETED")

    assert transitions(log_lines(), ride["id"]) == FULL_TRIP
    assert {pair: counter(*pair) - before[pair] for pair in FULL_TRIP} == {pair: 1 for pair in FULL_TRIP}
    assert await events_in_sql(db, ride["id"]) == FULL_TRIP
    line = next(line for line in log_lines() if line["msg"] == "ride_transition" and line["to_status"] == "COMPLETED")
    assert (line["user_id"], line["role"]) == (driver["user"].id, "driver")  # the request context of the commit
    assert "request_id" in line


async def test_a_transaction_that_rolls_back_emits_and_counts_nothing(db, log_lines, rider, insert_ride):
    ride = await insert_ride(rider, RideStatus.REQUESTED)
    ride_id = ride.id  # read now: a rollback expires the object
    before_lines, before_count = len(transitions(log_lines())), counter("REQUESTED", "CANCELLED")

    await rides_repo.add_event(db, ride_id, RideStatus.REQUESTED, RideStatus.CANCELLED, None)
    await db.rollback()

    assert len(transitions(log_lines())) == before_lines
    assert counter("REQUESTED", "CANCELLED") == before_count
    assert await events_in_sql(db, ride_id) == []


async def test_an_exception_in_the_middle_of_a_service_call_emits_nothing(client, db, log_lines, rider, driver, assign_ride, monkeypatch):
    ride = await assign_ride(rider, driver)
    trip = {"rider": rider, "driver": driver, "id": ride["id"]}
    await advance(client, trip, "IN_PROGRESS")
    before_lines, before_count = len(transitions(log_lines())), counter("IN_PROGRESS", "COMPLETED")

    async def broken(*args, **kwargs):
        raise RuntimeError("payment failed")

    monkeypatch.setattr(payments, "charge_ride", broken)
    response = await client.post(f"/rides/{ride['id']}/complete", headers=driver["headers"])

    assert response.status_code == 500
    assert len(transitions(log_lines())) == before_lines
    assert counter("IN_PROGRESS", "COMPLETED") == before_count
    assert (await events_in_sql(db, ride["id"]))[-1] == ("DRIVER_ARRIVED", "IN_PROGRESS")


async def test_a_rolled_back_savepoint_drops_only_its_own_events(db, log_lines, rider, insert_ride):
    ride = await insert_ride(rider, RideStatus.REQUESTED)
    ride_id = ride.id  # read now: a rollback expires the object
    before = len(transitions(log_lines()))

    await rides_repo.add_event(db, ride_id, RideStatus.REQUESTED, RideStatus.DRIVER_ASSIGNED, None)  # outside the savepoint
    try:
        async with db.begin_nested():
            await rides_repo.add_event(db, ride_id, RideStatus.DRIVER_ASSIGNED, RideStatus.DRIVER_ARRIVED, None)
            raise ValueError("undo the savepoint")
    except ValueError:
        pass
    await db.commit()

    assert transitions(log_lines())[before:] == [("REQUESTED", "DRIVER_ASSIGNED")]
    assert await events_in_sql(db, ride_id) == [("REQUESTED", "DRIVER_ASSIGNED")]


async def test_a_released_savepoint_inside_a_committed_transaction_emits_everything(db, log_lines, rider, insert_ride):
    ride = await insert_ride(rider, RideStatus.REQUESTED)
    ride_id = ride.id  # read now: a rollback expires the object
    before = len(transitions(log_lines()))

    await rides_repo.add_event(db, ride_id, RideStatus.REQUESTED, RideStatus.DRIVER_ASSIGNED, None)
    async with db.begin_nested():
        await rides_repo.add_event(db, ride_id, RideStatus.DRIVER_ASSIGNED, RideStatus.DRIVER_ARRIVED, None)
    assert len(transitions(log_lines())) == before  # the release is not a commit
    await db.commit()

    assert transitions(log_lines())[before:] == [("REQUESTED", "DRIVER_ASSIGNED"), ("DRIVER_ASSIGNED", "DRIVER_ARRIVED")]


async def test_a_session_closed_without_a_commit_emits_nothing_even_when_reused(test_engine, log_lines, rider, insert_ride):
    ride = await insert_ride(rider, RideStatus.REQUESTED)
    ride_id = ride.id  # read now: a rollback expires the object
    before = len(transitions(log_lines()))
    session = async_sessionmaker(test_engine, expire_on_commit=False)()

    await rides_repo.add_event(session, ride_id, RideStatus.REQUESTED, RideStatus.DRIVER_ASSIGNED, None)
    await session.close()
    await rides_repo.add_event(session, ride_id, RideStatus.REQUESTED, RideStatus.CANCELLED, None)  # the reused session
    await session.commit()
    await session.close()

    assert transitions(log_lines())[before:] == [("REQUESTED", "CANCELLED")]


async def test_twenty_simultaneous_accepts_emit_exactly_the_events_that_were_written(client, db, log_lines, rider, driver, put_online):
    await put_online(driver, RIDE_BODY["pickup_lat"], RIDE_BODY["pickup_lng"])
    created = await client.post("/rides", json=RIDE_BODY, headers=rider["headers"])
    offer_id = (await client.get("/drivers/me/offer", headers=driver["headers"])).json()["id"]
    ride_id = created.json()["id"]

    answers = await asyncio.gather(*[client.post(f"/offers/{offer_id}/accept", headers=driver["headers"]) for _ in range(20)])

    assert sorted(answer.status_code for answer in answers).count(200) == 1
    in_sql = await events_in_sql(db, ride_id)
    assert in_sql == [("none", "REQUESTED"), ("REQUESTED", "DRIVER_ASSIGNED")]
    assert transitions(log_lines(), ride_id) == in_sql


# --- observability never breaks a request ---


async def test_a_logger_that_raises_in_the_hook_does_not_fail_the_request(client, db, log_lines, rider, driver, put_online, monkeypatch):
    await put_online(driver, RIDE_BODY["pickup_lat"], RIDE_BODY["pickup_lng"])  # the offer keeps the ride REQUESTED: one transition
    def broken(*args, **kwargs):
        raise RuntimeError("the logger is broken")

    monkeypatch.setattr(hooks.logger, "info", broken)
    errors_before = hook_errors()

    response = await client.post("/rides", json=RIDE_BODY, headers=rider["headers"])

    assert response.status_code == 201
    assert await events_in_sql(db, response.json()["id"]) == [("none", "REQUESTED")]  # the ride stays changed
    assert hook_errors() == errors_before + 1
    assert any(line["msg"] == "observability_hook_error" and line["exc_type"] == "RuntimeError" for line in log_lines())


async def test_a_failure_in_the_access_line_does_not_change_the_response(client, log_lines, monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError("the logger is broken")

    monkeypatch.setattr(middleware.logger, "log", broken)
    errors_before = REGISTRY.get_sample_value("observability_errors_total", {"source": "middleware"}) or 0

    response = await client.get("/health")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert REGISTRY.get_sample_value("observability_errors_total", {"source": "middleware"}) == errors_before + 1


# --- the settlement and the other paths ---


async def test_a_completed_wallet_ride_emits_each_transition_once(client, db, log_lines, rider, driver, admin, put_online, accept_offer):
    funded = await client.post(
        f"/admin/wallets/{rider['user'].id}/adjust", json={"amount": 50000, "note": "funding"},
        headers={**admin["headers"], "Idempotency-Key": "fund-wallet-0001"},
    )
    assert funded.status_code == 201, funded.text
    await put_online(driver, RIDE_BODY["pickup_lat"], RIDE_BODY["pickup_lng"])
    created = await client.post("/rides", json={**RIDE_BODY, "payment_method": "wallet"}, headers=rider["headers"])
    assert created.status_code == 201, created.text
    ride = await accept_offer(driver)

    await advance(client, {"rider": rider, "driver": driver, "id": ride["id"]}, "COMPLETED")

    assert transitions(log_lines(), ride["id"]) == FULL_TRIP
    assert await events_in_sql(db, ride["id"]) == FULL_TRIP


async def test_a_late_cancellation_emits_its_transition_once(client, db, log_lines, rider, driver, assign_ride):
    ride = await assign_ride(rider, driver)
    trip = {"rider": rider, "driver": driver, "id": ride["id"]}
    await advance(client, trip, "DRIVER_ARRIVED")

    assert (await cancel(client, trip)).status_code == 200

    assert transitions(log_lines(), ride["id"]).count(("DRIVER_ARRIVED", "CANCELLED")) == 1
    assert transitions(log_lines(), ride["id"]) == await events_in_sql(db, ride["id"])


async def test_no_driver_found_from_the_sweeper_is_emitted_once_with_the_sweepers_context(client, db, log_lines, rider, driver, put_online):
    await put_online(driver, RIDE_BODY["pickup_lat"], RIDE_BODY["pickup_lng"])
    created = await client.post("/rides", json=RIDE_BODY, headers=rider["headers"])
    ride_id = created.json()["id"]
    await make_overdue(db, ride_id)
    before = len(log_lines())

    await start_background_task("sweeper", offers_service.expire_due_offers())

    new_lines = log_lines()[before:]
    assert transitions(new_lines, ride_id) == [("REQUESTED", "NO_DRIVER_FOUND")]
    assert await events_in_sql(db, ride_id) == [("none", "REQUESTED"), ("REQUESTED", "NO_DRIVER_FOUND")]
    assert new_lines and all(line.get("component") == "sweeper" and "request_id" not in line for line in new_lines)
