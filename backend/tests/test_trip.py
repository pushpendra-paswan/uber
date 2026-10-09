import asyncio
import json

import pytest
import pytest_asyncio
from redis.exceptions import RedisError
from sqlalchemy import func, select

from app.database import redis_client
from app.models import Ride, RideEvent, RideStatus
from app.repositories import events
from test_matching import offers_of, online_at, request_ride  # noqa: F401  (the last two are fixtures)
from test_offers import ride_events, ride_status
from test_rides import RIDE_BODY
from test_tracking import spy  # noqa: F401  (a fixture: listens on the raw Redis channel)
from test_websocket import assert_silent, open_socket  # noqa: F401  (open_socket is a fixture)

# The steps of a normal trip, from DRIVER_ASSIGNED, and the statuses they lead to.
STEPS = [("arrive", "DRIVER_ARRIVED"), ("start", "IN_PROGRESS"), ("complete", "COMPLETED")]


@pytest_asyncio.fixture
async def trip(rider, driver, assign_ride):
    """A rider and a driver with a DRIVER_ASSIGNED ride. Ask for `spy` after this fixture, so the spy
    subscribes after the assignment events were published and does not see them."""
    ride = await assign_ride(rider, driver)
    return {"rider": rider, "driver": driver, "id": ride["id"]}


async def call(client, method: str, url: str, who: dict | None = None, **options):
    response = await client.request(method, url, headers=who["headers"] if who else {}, **options)
    # The trip code leaves the backend only through GET /rides/{id}/otp. (A 422 names the field "otp".)
    if not url.endswith("/otp") and response.status_code != 422:
        assert "otp" not in response.text, response.text
    return response


async def step(client, trip: dict, name: str, otp: str = "1234", who: str = "driver"):
    body = {"otp": otp} if name == "start" else None
    return await call(client, "POST", f"/rides/{trip['id']}/{name}", trip[who], json=body)


async def advance(client, trip: dict, status: str) -> None:
    for name, reached in STEPS:
        response = await step(client, trip, name)
        assert response.status_code == 200, response.text
        if reached == status:
            return
    raise AssertionError(f"{status} is not on the trip")


async def cancel(client, trip: dict, who: str = "rider"):
    return await call(client, "POST", f"/rides/{trip['id']}/cancel", trip[who])


async def get_otp(client, trip: dict, who: dict | None = None):
    return await call(client, "GET", f"/rides/{trip['id']}/otp", who or trip["rider"])


async def stored_otp(db, ride_id: int) -> str | None:
    # A column, not the entity, so the session's identity map cannot show an older copy.
    return await db.scalar(select(Ride.otp).where(Ride.id == ride_id))


async def event_count(db, ride_id: int) -> int:
    return await db.scalar(select(func.count()).select_from(RideEvent).where(RideEvent.ride_id == ride_id))


async def get(ws) -> dict:
    """Receives one WebSocket message and checks that the trip code is not in it."""
    raw = await asyncio.wait_for(ws.recv(), 2)
    assert "otp" not in raw, raw
    return json.loads(raw)


def ride_updated(trip: dict, status: str) -> dict:
    return {"type": "ride_updated", "data": {"ride_id": trip["id"], "status": status}}


# --- the code is stored per ride and never leaves except through the rider-only endpoint ---


async def test_the_code_is_stored_on_accept_and_is_in_no_response_or_message(
    client, db, open_socket, trip, admin
):
    rider_ws = await open_socket(trip["rider"])
    driver_ws = await open_socket(trip["driver"])
    assert await stored_otp(db, trip["id"]) == "1234"

    for who in (trip["rider"], trip["driver"], admin):
        assert (await call(client, "GET", f"/rides/{trip['id']}", who)).status_code == 200
        assert (await call(client, "GET", f"/rides/{trip['id']}/events", who)).status_code == 200
    for who in (trip["rider"], trip["driver"]):
        assert (await call(client, "GET", "/rides/active", who)).status_code == 200

    for name, status in STEPS:
        response = await step(client, trip, name)
        assert response.status_code == 200
        assert set(response.json()) == {
            "id", "rider_id", "driver_id", "pickup_lat", "pickup_lng", "pickup_address", "dropoff_lat", "dropoff_lng",
            "dropoff_address", "status", "distance_m", "duration_s", "fare_estimate", "final_fare", "actual_distance_m",
            "actual_duration_s", "fare_breakdown", "created_at", "started_at", "completed_at",
        }
        assert await get(rider_ws) == ride_updated(trip, status)
        assert await get(driver_ws) == ride_updated(trip, status)
    for who in (trip["rider"], trip["driver"], admin):
        assert (await call(client, "GET", f"/rides/{trip['id']}", who)).status_code == 200
        assert (await call(client, "GET", f"/rides/{trip['id']}/events", who)).status_code == 200


async def test_the_cancel_response_and_messages_have_no_code(client, open_socket, trip):
    rider_ws = await open_socket(trip["rider"])
    driver_ws = await open_socket(trip["driver"])

    response = await cancel(client, trip)

    assert response.status_code == 200
    assert await get(rider_ws) == ride_updated(trip, "CANCELLED")
    assert await get(driver_ws) == ride_updated(trip, "CANCELLED")


async def test_the_accept_response_has_no_code(client, put_online, accept_offer, rider, driver):
    await put_online(driver, RIDE_BODY["pickup_lat"], RIDE_BODY["pickup_lng"])
    await call(client, "POST", "/rides", rider, json=RIDE_BODY)

    accepted = await accept_offer(driver)

    assert accepted["status"] == "DRIVER_ASSIGNED"
    assert "otp" not in accepted


# --- GET /rides/{id}/otp ---


@pytest.mark.parametrize("status", ["DRIVER_ASSIGNED", "DRIVER_ARRIVED"])
async def test_the_rider_reads_the_code_while_the_driver_is_assigned_or_has_arrived(client, trip, status):
    if status == "DRIVER_ARRIVED":
        await advance(client, trip, status)

    response = await get_otp(client, trip)

    assert response.status_code == 200
    assert response.json() == {"otp": "1234"}


@pytest.mark.parametrize("status", ["IN_PROGRESS", "COMPLETED", "CANCELLED"])
async def test_the_code_is_not_available_after_the_trip_starts_or_ends(client, trip, status):
    if status == "CANCELLED":
        await cancel(client, trip)
    else:
        await advance(client, trip, status)

    response = await get_otp(client, trip)

    assert response.status_code == 409
    assert response.json()["detail"] == "Trip code is not available"


async def test_the_code_is_not_available_while_the_ride_is_only_requested(client, rider, online_at, request_ride):
    await online_at(500)
    ride = await request_ride(rider)

    response = await call(client, "GET", f"/rides/{ride['id']}/otp", rider)

    assert ride["status"] == "REQUESTED"
    assert response.status_code == 409


async def test_who_may_read_the_code(client, trip, admin, make_user):
    other_rider = await make_user("rider")

    assert (await get_otp(client, trip, other_rider)).status_code == 404
    assert (await call(client, "GET", "/rides/99999/otp", trip["rider"])).status_code == 404
    assert (await get_otp(client, trip, trip["driver"])).status_code == 403
    assert (await get_otp(client, trip, admin)).status_code == 403
    assert (await call(client, "GET", f"/rides/{trip['id']}/otp")).status_code == 401


# --- start ---


async def test_start_with_the_right_code(client, db, trip):
    await advance(client, trip, "DRIVER_ARRIVED")

    response = await step(client, trip, "start")

    assert response.status_code == 200
    assert response.json()["status"] == "IN_PROGRESS"
    assert response.json()["started_at"] is not None
    assert await stored_otp(db, trip["id"]) is None
    assert (await get_otp(client, trip)).status_code == 409
    assert (await step(client, trip, "start")).status_code == 409


async def test_a_wrong_code_is_400_and_changes_and_publishes_nothing(client, db, open_socket, trip, spy):
    await advance(client, trip, "DRIVER_ARRIVED")
    rider_ws = await open_socket(trip["rider"])
    driver_ws = await open_socket(trip["driver"])
    await spy()  # the arrive event is not what this test is about
    before = await ride_events(db, trip["id"])

    response = await step(client, trip, "start", otp="0000")

    assert response.status_code == 400
    assert response.json()["detail"] == "Incorrect trip code"
    assert await ride_status(db, trip["id"]) == "DRIVER_ARRIVED"
    assert await ride_events(db, trip["id"]) == before
    assert await stored_otp(db, trip["id"]) == "1234"
    assert await spy() == []
    await assert_silent(rider_ws)
    await assert_silent(driver_ws)
    assert (await step(client, trip, "start")).status_code == 200


async def test_a_ride_without_a_stored_code_cannot_be_started(client, db, rider, driver, insert_ride):
    ride = await insert_ride(rider, RideStatus.DRIVER_ARRIVED, driver, with_otp=False)

    response = await call(client, "POST", f"/rides/{ride.id}/start", driver, json={"otp": "1234"})

    assert response.status_code == 400
    assert await ride_status(db, ride.id) == "DRIVER_ARRIVED"


@pytest.mark.parametrize(
    "body",
    [None, {}, {"otp": 1234}, {"otp": "12"}, {"otp": "abcd"}, {"otp": "12345"}, {"otp": " 123"}, {"otp": "1234\n"}, {"otp": "１２３４"}],
    ids=["no-body", "no-field", "number", "two-digits", "letters", "five-digits", "space", "newline", "fullwidth-digits"],
)
async def test_a_badly_formed_code_is_422_and_changes_nothing(client, db, trip, body):
    await advance(client, trip, "DRIVER_ARRIVED")
    before = await ride_events(db, trip["id"])

    response = await call(client, "POST", f"/rides/{trip['id']}/start", trip["driver"], json=body)

    assert response.status_code == 422
    assert await ride_status(db, trip["id"]) == "DRIVER_ARRIVED"
    assert await ride_events(db, trip["id"]) == before
    assert await stored_otp(db, trip["id"]) == "1234"


@pytest.mark.parametrize("otp", ["1234", "0000"])
@pytest.mark.parametrize("status", ["DRIVER_ASSIGNED", "IN_PROGRESS", "COMPLETED", "CANCELLED"])
async def test_start_in_any_other_state_is_409_whatever_the_code(client, db, trip, status, otp):
    if status == "CANCELLED":
        await cancel(client, trip)
    elif status != "DRIVER_ASSIGNED":
        await advance(client, trip, status)
    before = await ride_events(db, trip["id"])

    response = await step(client, trip, "start", otp=otp)

    assert response.status_code == 409
    assert await ride_status(db, trip["id"]) == status
    assert await ride_events(db, trip["id"]) == before


async def test_who_may_start(client, trip, make_user):
    await advance(client, trip, "DRIVER_ARRIVED")
    other_driver = await make_user("driver")
    body = {"otp": "1234"}

    assert (await call(client, "POST", f"/rides/{trip['id']}/start", other_driver, json=body)).status_code == 404
    assert (await call(client, "POST", f"/rides/{trip['id']}/start", trip["rider"], json=body)).status_code == 403
    assert (await call(client, "POST", f"/rides/{trip['id']}/start", json=body)).status_code == 401


# --- events ---


async def test_every_status_change_reaches_both_people_and_nobody_else(client, open_socket, trip, make_user):
    rider_ws = await open_socket(trip["rider"])
    driver_ws = await open_socket(trip["driver"])
    stranger_ws = [await open_socket(await make_user("rider")), await open_socket(await make_user("driver"))]

    for name, status in STEPS:
        assert (await step(client, trip, name)).status_code == 200
        assert await get(rider_ws) == ride_updated(trip, status)
        assert await get(driver_ws) == ride_updated(trip, status)

    await assert_silent(rider_ws)
    await assert_silent(driver_ws)
    for ws in stranger_ws:
        await assert_silent(ws)


@pytest.mark.parametrize("from_status", ["DRIVER_ASSIGNED", "DRIVER_ARRIVED"])
@pytest.mark.parametrize("canceller", ["rider", "driver"])
async def test_either_side_can_cancel_before_the_trip_starts(client, db, open_socket, trip, canceller, from_status):
    if from_status == "DRIVER_ARRIVED":
        await advance(client, trip, from_status)
    rider_ws = await open_socket(trip["rider"])
    driver_ws = await open_socket(trip["driver"])

    response = await cancel(client, trip, canceller)

    assert response.status_code == 200
    assert response.json()["status"] == "CANCELLED"
    assert await stored_otp(db, trip["id"]) is None
    assert (await ride_events(db, trip["id"]))[-1] == (from_status, "CANCELLED", trip[canceller]["user"].id)
    assert await get(rider_ws) == ride_updated(trip, "CANCELLED")
    assert await get(driver_ws) == ride_updated(trip, "CANCELLED")
    await assert_silent(rider_ws)
    await assert_silent(driver_ws)


@pytest.mark.parametrize("canceller", ["rider", "driver"])
async def test_nobody_can_cancel_during_the_trip_and_nothing_is_published(client, db, trip, spy, canceller):
    await advance(client, trip, "IN_PROGRESS")
    await spy()

    response = await cancel(client, trip, canceller)

    assert response.status_code == 409
    assert await ride_status(db, trip["id"]) == "IN_PROGRESS"
    assert await spy() == []


@pytest.mark.parametrize("canceller", ["rider", "driver"])
async def test_nobody_can_cancel_after_the_trip_ended(client, trip, spy, canceller):
    await advance(client, trip, "COMPLETED")
    await spy()

    response = await cancel(client, trip, canceller)

    assert response.status_code == 409
    assert await spy() == []


async def test_cancelling_a_requested_ride_closes_the_offer_and_tells_only_the_rider(
    client, db, open_socket, rider, online_at, request_ride
):
    driver = await online_at(500)
    rider_ws = await open_socket(rider)
    driver_ws = await open_socket(driver)
    ride = await request_ride(rider)
    offer_created = await get(driver_ws)
    assert offer_created["type"] == "offer_created"

    response = await call(client, "POST", f"/rides/{ride['id']}/cancel", rider)

    assert response.status_code == 200
    assert await ride_status(db, ride["id"]) == "CANCELLED"
    assert await get(driver_ws) == {
        "type": "offer_closed",
        "data": {"offer_id": offer_created["data"]["offer_id"], "ride_id": ride["id"], "reason": "ride_cancelled"},
    }
    assert await get(rider_ws) == ride_updated({"id": ride["id"]}, "CANCELLED")
    await assert_silent(driver_ws)  # no ride_updated: no driver is assigned to this ride
    await assert_silent(rider_ws)


# --- a whole trip ---


async def test_a_whole_trip_through_the_api(client, db, rider, driver, assign_ride, request_ride):
    ride = await assign_ride(rider, driver)
    trip = {"rider": rider, "driver": driver, "id": ride["id"]}

    assert (await step(client, trip, "arrive")).status_code == 200
    assert (await get_otp(client, trip)).json() == {"otp": "1234"}
    started = await step(client, trip, "start", otp=(await get_otp(client, trip)).json()["otp"])
    completed = await step(client, trip, "complete")

    assert (started.status_code, completed.status_code) == (200, 200)
    assert started.json()["started_at"] is not None
    assert completed.json()["completed_at"] is not None
    assert completed.json()["status"] == "COMPLETED"
    assert await ride_events(db, ride["id"]) == [
        (None, "REQUESTED", rider["user"].id),
        ("REQUESTED", "DRIVER_ASSIGNED", driver["user"].id),
        ("DRIVER_ASSIGNED", "DRIVER_ARRIVED", driver["user"].id),
        ("DRIVER_ARRIVED", "IN_PROGRESS", driver["user"].id),
        ("IN_PROGRESS", "COMPLETED", driver["user"].id),
    ]
    assert (await client.get("/rides/active", headers=rider["headers"])).status_code == 404

    # Both people are free again: the same driver is offered the rider's next ride.
    second = await request_ride(rider)
    assert await offers_of(db, second["id"]) == [(driver["driver"].id, "PENDING")]


async def test_a_driver_who_cancelled_is_offered_the_next_ride(client, db, trip, request_ride):
    await advance(client, trip, "DRIVER_ARRIVED")
    assert (await cancel(client, trip, "driver")).status_code == 200

    second = await request_ride(trip["rider"])

    assert await offers_of(db, second["id"]) == [(trip["driver"]["driver"].id, "PENDING")]


# --- ordering and failures ---


async def test_events_go_out_after_the_commit(client, test_engine, open_socket, trip, monkeypatch):
    rider_ws = await open_socket(trip["rider"])
    seen = []
    real_publish = events.publish

    async def spy_on_publish(user_id, type, data):
        # A new connection, so only committed data is visible to it.
        async with test_engine.connect() as connection:
            status = await connection.scalar(select(Ride.status).where(Ride.id == data["ride_id"]))
        seen.append((type, data["status"], status.value))
        return await real_publish(user_id, type, data)

    monkeypatch.setattr(events, "publish", spy_on_publish)

    for name, status in STEPS:
        assert (await step(client, trip, name)).status_code == 200
        event = await get(rider_ws)
        # The page reacts to the event with a REST read, which already shows the new status.
        assert event == ride_updated(trip, status)
        assert (await call(client, "GET", f"/rides/{trip['id']}", trip["rider"])).json()["status"] == status

    # Two publishes per change (rider, then driver); each already found the new status in the database.
    assert seen == [("ride_updated", status, status) for _, status in STEPS for _ in range(2)]


async def test_a_failed_publish_does_not_fail_or_undo_arrive_and_cancel(client, db, trip, monkeypatch):
    async def broken_publish(*args):
        raise RedisError("publish failed")

    monkeypatch.setattr(redis_client, "publish", broken_publish)

    arrived = await step(client, trip, "arrive")
    cancelled = await cancel(client, trip)

    assert (arrived.status_code, cancelled.status_code) == (200, 200)
    assert await ride_status(db, trip["id"]) == "CANCELLED"
    assert [event[1] for event in await ride_events(db, trip["id"])][-2:] == ["DRIVER_ARRIVED", "CANCELLED"]


# --- simple races (the heavy ones belong to M4) ---


async def test_two_simultaneous_starts_give_one_winner(client, db, trip):
    await advance(client, trip, "DRIVER_ARRIVED")

    responses = await asyncio.gather(step(client, trip, "start"), step(client, trip, "start"))

    assert sorted(response.status_code for response in responses) == [200, 409]
    started = await db.scalar(
        select(func.count()).select_from(RideEvent).where(RideEvent.ride_id == trip["id"], RideEvent.to_status == RideStatus.IN_PROGRESS)
    )
    assert started == 1


@pytest.mark.parametrize("round_number", range(5))
async def test_a_start_racing_a_cancel_has_exactly_one_winner(client, db, rider, driver, assign_ride, round_number):
    ride = await assign_ride(rider, driver)
    trip = {"rider": rider, "driver": driver, "id": ride["id"]}
    await advance(client, trip, "DRIVER_ARRIVED")
    # Alternate who cancels, so both sides race the start.
    canceller = "rider" if round_number % 2 == 0 else "driver"

    start, cancelled = await asyncio.gather(step(client, trip, "start"), cancel(client, trip, canceller))

    assert sorted([start.status_code, cancelled.status_code]) == [200, 409]
    final = await ride_status(db, trip["id"])
    last_event = (await ride_events(db, trip["id"]))[-1]
    assert await stored_otp(db, trip["id"]) is None
    if start.status_code == 200:
        assert (cancelled.status_code, final, last_event[1]) == (409, "IN_PROGRESS", "IN_PROGRESS")
    else:
        assert (start.status_code, cancelled.status_code, final, last_event[1]) == (409, 200, "CANCELLED", "CANCELLED")
    assert await event_count(db, trip["id"]) == 4  # REQUESTED, DRIVER_ASSIGNED, DRIVER_ARRIVED, and the one winner
