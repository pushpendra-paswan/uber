import asyncio
import json
import time

import pytest
import pytest_asyncio

from app.database import redis_client
from app.models import RideStatus
from test_rides import RIDE_BODY
from test_websocket import assert_silent, open_socket, receive  # noqa: F401  (open_socket is a fixture)

# Written out by hand, not imported from the app, so a wrong change to the channel name fails the tests.
CHANNEL = "ws:events:1"
LAT = RIDE_BODY["pickup_lat"]
LNG = RIDE_BODY["pickup_lng"]

# The driver endpoints that lead to each status, in order.
STEPS_TO = {
    "DRIVER_ASSIGNED": [],
    "DRIVER_ARRIVED": ["arrive"],
    "IN_PROGRESS": ["arrive", "start"],
    "COMPLETED": ["arrive", "start", "complete"],
}


@pytest_asyncio.fixture
async def spy():
    """Listens on the raw Redis channel, so "nothing was published to anyone" does not depend on any socket."""
    before = (await redis_client.pubsub_numsub(CHANNEL))[0][1]
    pubsub = redis_client.pubsub(ignore_subscribe_messages=True)
    await pubsub.subscribe(CHANNEL)
    deadline = time.monotonic() + 5
    while (await redis_client.pubsub_numsub(CHANNEL))[0][1] != before + 1:
        assert time.monotonic() < deadline, "the spy never subscribed"
        await asyncio.sleep(0.02)

    async def published(seconds: float = 0.5) -> list[dict]:
        """Everything published in the next `seconds`."""
        found = []
        deadline = time.monotonic() + seconds
        while (left := deadline - time.monotonic()) > 0:
            message = await pubsub.get_message(timeout=left)
            if message is not None:
                found.append(json.loads(message["data"]))
        return found

    yield published
    await pubsub.aclose()


@pytest_asyncio.fixture
async def assigned(client, rider, driver, put_online):
    """A rider and a driver with a DRIVER_ASSIGNED ride, made by matching."""
    await put_online(driver, LAT, LNG)
    response = await client.post("/rides", json=RIDE_BODY, headers=rider["headers"])
    assert response.status_code == 201
    assert response.json()["status"] == "DRIVER_ASSIGNED"
    return {"rider": rider, "driver": driver, "ride_id": response.json()["id"]}


async def ping(client, who: dict, lat: float = LAT, lng: float = LNG) -> dict:
    response = await client.post("/drivers/me/location", json={"lat": lat, "lng": lng}, headers=who["headers"])
    assert response.status_code == 200, response.text
    return response.json()


async def advance(client, trip: dict, status: str) -> None:
    for step in STEPS_TO[status]:
        response = await client.post(f"/rides/{trip['ride_id']}/{step}", headers=trip["driver"]["headers"])
        assert response.status_code == 200, response.text


async def cancel(client, trip: dict) -> None:
    response = await client.post(f"/rides/{trip['ride_id']}/cancel", headers=trip["rider"]["headers"])
    assert response.status_code == 200, response.text


async def get_driver_details(client, ride_id: int, headers: dict | None):
    return await client.get(f"/rides/{ride_id}/driver", headers=headers or {})


# --- events ---


@pytest.mark.parametrize("status", ["DRIVER_ASSIGNED", "DRIVER_ARRIVED", "IN_PROGRESS"])
async def test_a_ping_during_an_active_ride_reaches_the_rider_once(client, open_socket, assigned, status):
    await advance(client, assigned, status)
    rider_ws = await open_socket(assigned["rider"])

    response = await ping(client, assigned["driver"], LAT + 0.002, LNG + 0.003)

    event = await receive(rider_ws)
    assert event["type"] == "driver_location"
    assert set(event["data"]) == {"ride_id", "lat", "lng", "updated_at"}
    assert event["data"] == {
        "ride_id": assigned["ride_id"],
        "lat": LAT + 0.002,
        "lng": LNG + 0.003,
        "updated_at": response["updated_at"],
    }
    await assert_silent(rider_ws)  # exactly one


@pytest.mark.parametrize("ending", ["completed", "cancelled"])
async def test_a_ping_after_the_ride_ended_publishes_nothing(client, assigned, spy, ending):
    if ending == "completed":
        await advance(client, assigned, "COMPLETED")
    else:
        await cancel(client, assigned)

    await ping(client, assigned["driver"])

    assert await spy() == []


async def test_a_driver_with_no_ride_publishes_nothing(client, driver, put_online, spy):
    await put_online(driver, LAT, LNG)

    await ping(client, driver)

    assert await spy() == []


async def test_only_the_rides_rider_receives_and_both_of_their_tabs_do(client, open_socket, assigned, make_user):
    other_rider = await make_user("rider")
    first_tab = await open_socket(assigned["rider"])
    second_tab = await open_socket(assigned["rider"])
    other_ws = await open_socket(other_rider)
    driver_ws = await open_socket(assigned["driver"])

    response = await ping(client, assigned["driver"])

    for ws in (first_tab, second_tab):
        assert (await receive(ws))["data"]["updated_at"] == response["updated_at"]
    await assert_silent(other_ws)
    await assert_silent(driver_ws)


async def test_a_ping_while_offline_is_409_and_publishes_nothing(client, rider, driver, put_online, insert_ride, spy):
    await put_online(driver, LAT, LNG)
    await insert_ride(rider, RideStatus.DRIVER_ASSIGNED, driver)
    await redis_client.delete(f"driver:{driver['driver'].id}:presence")

    response = await client.post("/drivers/me/location", json={"lat": LAT, "lng": LNG}, headers=driver["headers"])

    assert response.status_code == 409
    assert await spy() == []


async def test_a_ping_from_an_unapproved_driver_is_403_and_publishes_nothing(client, rider, make_user, insert_ride, spy):
    pending = await make_user("driver", approved=False)
    await insert_ride(rider, RideStatus.DRIVER_ASSIGNED, pending)

    response = await client.post("/drivers/me/location", json={"lat": LAT, "lng": LNG}, headers=pending["headers"])

    assert response.status_code == 403
    assert await spy() == []


async def test_going_online_during_a_ride_publishes_nothing_but_the_next_ping_does(client, assigned, spy):
    response = await client.post("/drivers/me/online", json={"lat": LAT, "lng": LNG}, headers=assigned["driver"]["headers"])
    assert response.status_code == 200
    assert await spy() == []

    await ping(client, assigned["driver"])

    events = await spy()
    assert [event["type"] for event in events] == ["driver_location"]


async def test_fifty_pings_arrive_in_order(client, open_socket, assigned):
    rider_ws = await open_socket(assigned["rider"])
    sent = [LAT + i * 0.00001 for i in range(50)]

    for lat in sent:
        await ping(client, assigned["driver"], lat, LNG)

    received = [(await receive(rider_ws))["data"]["lat"] for _ in range(50)]
    assert received == sent


async def test_the_ping_response_is_unchanged(client, assigned, driver):
    response = await ping(client, driver, LAT + 0.001, LNG + 0.001)

    assert set(response) == {"online", "lat", "lng", "updated_at"}
    assert response["online"] is True
    assert (response["lat"], response["lng"]) == (LAT + 0.001, LNG + 0.001)
    assert abs(response["updated_at"] - time.time()) < 5


# --- GET /rides/{id}/driver ---


async def test_who_may_see_the_driver_of_a_ride(client, assigned, make_user, insert_ride, admin):
    ride_id = assigned["ride_id"]
    other_rider = await make_user("rider")
    other_driver = await make_user("driver")
    no_driver_ride = await insert_ride(other_rider, RideStatus.NO_DRIVER_FOUND)

    cases = {
        "the ride's rider": (ride_id, assigned["rider"]["headers"], 200),
        "another rider": (ride_id, other_rider["headers"], 404),
        "the assigned driver": (ride_id, assigned["driver"]["headers"], 200),
        "an unassigned driver": (ride_id, other_driver["headers"], 404),
        "an admin": (ride_id, admin["headers"], 200),
        "no token": (ride_id, None, 401),
        "a ride without a driver": (no_driver_ride.id, other_rider["headers"], 404),
        "an unknown ride": (999999, other_rider["headers"], 404),
    }
    for name, (id_, headers, expected) in cases.items():
        response = await get_driver_details(client, id_, headers)
        assert response.status_code == expected, f"{name}: {response.text}"


async def test_the_details_hold_only_name_vehicle_and_location(client, db, assigned):
    driver = assigned["driver"]
    driver["user"].phone = "9999999999"
    await db.commit()
    await ping(client, driver, LAT + 0.002, LNG + 0.003)

    response = await get_driver_details(client, assigned["ride_id"], assigned["rider"]["headers"])

    body = response.json()
    assert set(body) == {"driver_id", "name", "vehicle", "location"}
    assert body["driver_id"] == driver["driver"].id
    assert body["name"] == "Test driver"
    assert set(body["vehicle"]) == {"id", "plate_number", "model", "color", "vehicle_type"}
    assert (body["vehicle"]["model"], body["vehicle"]["color"]) == ("Swift", "white")
    assert set(body["location"]) == {"lat", "lng", "updated_at"}
    assert abs(body["location"]["lat"] - (LAT + 0.002)) < 0.0001
    assert abs(body["location"]["lng"] - (LNG + 0.003)) < 0.0001
    for secret in (driver["user"].email, driver["driver"].license_number, "9999999999"):
        assert secret not in response.text


async def test_the_location_is_null_when_the_presence_is_gone_but_the_rest_stays(client, assigned):
    await redis_client.delete(f"driver:{assigned['driver']['driver'].id}:presence")

    response = await get_driver_details(client, assigned["ride_id"], assigned["rider"]["headers"])

    assert response.status_code == 200
    body = response.json()
    assert body["location"] is None
    assert body["name"] == "Test driver"
    assert body["vehicle"]["model"] == "Swift"


@pytest.mark.parametrize("ending", ["completed", "cancelled"])
async def test_a_finished_ride_shows_no_location_even_if_the_driver_is_online(client, assigned, ending):
    if ending == "completed":
        await advance(client, assigned, "COMPLETED")
    else:
        await cancel(client, assigned)
    presence = await client.get("/drivers/me/presence", headers=assigned["driver"]["headers"])
    assert presence.json()["online"] is True

    response = await get_driver_details(client, assigned["ride_id"], assigned["rider"]["headers"])

    assert response.status_code == 200
    body = response.json()
    assert body["location"] is None
    assert body["name"] == "Test driver"
    assert body["vehicle"]["model"] == "Swift"
