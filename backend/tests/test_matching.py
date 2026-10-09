import pytest
import pytest_asyncio
from redis.exceptions import ConnectionError as RedisConnectionError
from sqlalchemy import func, select

from app.config import settings
from app.database import redis_client
from app.models import Ride, RideEvent, RideOffer, RideStatus, VerificationStatus
from app.repositories import drivers as drivers_repo
from app.services import matching
from test_rides import RIDE_BODY

GEO_KEY = "drivers:geo"

# Every offset is due north of the pickup (the city center), where one degree of latitude is about 111.2 km.
# Test distances stay at least 200 m away from the 3 km radius, because Redis and this formula differ a little.
PICKUP_LNG = RIDE_BODY["pickup_lng"]


def north(meters: float) -> float:
    return RIDE_BODY["pickup_lat"] + meters / 111_200


@pytest_asyncio.fixture
async def online_at(make_user, put_online):
    """Returns a function that creates an approved driver and puts them online `meters` north of the pickup."""

    async def create(meters: float) -> dict:
        who = await make_user("driver")
        await put_online(who, north(meters), PICKUP_LNG)
        return who

    return create


@pytest_asyncio.fixture
async def request_ride(client):
    async def create(who: dict) -> dict:
        response = await client.post("/rides", json=RIDE_BODY, headers=who["headers"])
        assert response.status_code == 201
        return response.json()

    return create


async def offers_of(db, ride_id: int) -> list[tuple]:
    """(driver id, status) of every offer of the ride, oldest first. Columns only, so the session's identity map cannot hide a change."""
    result = await db.execute(
        select(RideOffer.driver_id, RideOffer.status).where(RideOffer.ride_id == ride_id).order_by(RideOffer.id)
    )
    return [(driver_id, status.value) for driver_id, status in result.all()]


async def events_of(client, who: dict, ride_id: int) -> list[tuple]:
    response = await client.get(f"/rides/{ride_id}/events", headers=who["headers"])
    return [(event["from_status"], event["to_status"], event["actor_user_id"]) for event in response.json()]


async def test_the_nearest_driver_gets_the_offer_and_accepting_assigns_the_ride(
    client, db, rider, online_at, request_ride, accept_offer
):
    # Created far to near, so the nearest is neither the first nor the lowest id.
    far = await online_at(2500)
    middle = await online_at(1200)
    nearest = await online_at(500)

    ride = await request_ride(rider)

    # Matching only offers: the ride stays REQUESTED and has no driver until the driver accepts.
    assert ride["status"] == "REQUESTED"
    assert ride["driver_id"] is None
    assert await offers_of(db, ride["id"]) == [(nearest["driver"].id, "PENDING")]
    assert await events_of(client, rider, ride["id"]) == [(None, "REQUESTED", rider["user"].id)]
    assert (await client.get("/drivers/me/offer", headers=nearest["headers"])).status_code == 200
    for other in (middle, far):
        assert (await client.get("/drivers/me/offer", headers=other["headers"])).status_code == 404

    accepted = await accept_offer(nearest)

    assert accepted["status"] == "DRIVER_ASSIGNED"
    assert accepted["driver_id"] == nearest["driver"].id
    assert accepted["driver_id"] != nearest["user"].id  # drivers.id, not users.id
    assert await events_of(client, rider, ride["id"]) == [
        (None, "REQUESTED", rider["user"].id),
        ("REQUESTED", "DRIVER_ASSIGNED", nearest["user"].id),
    ]
    active = await client.get("/rides/active", headers=nearest["headers"])
    assert active.status_code == 200
    assert active.json()["id"] == ride["id"]
    for other in (middle, far):
        assert (await client.get("/rides/active", headers=other["headers"])).status_code == 404


async def test_a_driver_outside_the_radius_is_not_matched(client, rider, online_at, request_ride):
    await online_at(3300)

    ride = await request_ride(rider)

    assert ride["status"] == "NO_DRIVER_FOUND"
    assert ride["driver_id"] is None
    assert await events_of(client, rider, ride["id"]) == [
        (None, "REQUESTED", rider["user"].id),
        ("REQUESTED", "NO_DRIVER_FOUND", None),
    ]
    # A ride that found nobody is over: no active ride, and the rider can ask again at once.
    assert (await client.get("/rides/active", headers=rider["headers"])).status_code == 404
    again = await client.post("/rides", json=RIDE_BODY, headers=rider["headers"])
    assert again.status_code == 201
    assert again.json()["status"] == "NO_DRIVER_FOUND"


async def test_a_driver_just_inside_the_radius_is_offered_the_ride(db, rider, online_at, request_ride):
    inside = await online_at(2800)

    ride = await request_ride(rider)

    assert ride["status"] == "REQUESTED"
    assert await offers_of(db, ride["id"]) == [(inside["driver"].id, "PENDING")]


async def test_nobody_online_means_no_driver_found(rider, request_ride):
    ride = await request_ride(rider)

    assert ride["status"] == "NO_DRIVER_FOUND"


async def test_a_stale_geo_member_is_skipped_and_removed(db, rider, online_at, request_ride):
    stale = await online_at(500)
    live = await online_at(1500)
    await redis_client.delete(f"driver:{stale['driver'].id}:presence")  # what the 30 s TTL does

    ride = await request_ride(rider)

    assert await offers_of(db, ride["id"]) == [(live["driver"].id, "PENDING")]
    assert await redis_client.zscore(GEO_KEY, str(stale["driver"].id)) is None
    assert await redis_client.zscore(GEO_KEY, str(live["driver"].id)) is not None


async def test_a_driver_with_an_active_ride_is_skipped(db, make_user, online_at, request_ride, accept_offer):
    near = await online_at(500)
    far = await online_at(1500)
    first_rider = await make_user("rider")
    second_rider = await make_user("rider")

    first = await request_ride(first_rider)
    assert (await accept_offer(near))["driver_id"] == near["driver"].id
    second = await request_ride(second_rider)

    assert await offers_of(db, first["id"]) == [(near["driver"].id, "ACCEPTED")]
    assert await offers_of(db, second["id"]) == [(far["driver"].id, "PENDING")]


async def test_a_driver_holding_an_offer_is_not_offered_a_second_ride(db, make_user, online_at, request_ride, accept_offer):
    only = await online_at(500)
    first_rider = await make_user("rider")
    second_rider = await make_user("rider")

    first = await request_ride(first_rider)
    second = await request_ride(second_rider)

    # The driver is still deciding on the first ride, so the second finds nobody.
    assert first["status"] == "REQUESTED"
    assert second["status"] == "NO_DRIVER_FOUND"
    assert await offers_of(db, second["id"]) == []

    # And with the first ride accepted the driver is busy for the same reason.
    await accept_offer(only)
    third = await request_ride(await make_user("rider"))
    assert third["status"] == "NO_DRIVER_FOUND"


async def test_a_driver_who_is_not_approved_is_never_matched(db, rider, online_at, request_ride):
    driver = await online_at(500)

    # Redis is left alone: the driver stays online there, only the database changes.
    for status in (VerificationStatus.pending, VerificationStatus.rejected):
        driver["driver"].verification_status = status
        await db.commit()
        assert (await request_ride(rider))["status"] == "NO_DRIVER_FOUND"

    driver["driver"].verification_status = VerificationStatus.approved
    await db.commit()
    ride = await request_ride(rider)
    assert await offers_of(db, ride["id"]) == [(driver["driver"].id, "PENDING")]


async def test_equal_distance_goes_to_the_lower_driver_id(db, make_user, put_online, rider, request_ride):
    drivers = [await make_user("driver") for _ in range(10)]
    assert (drivers[8]["driver"].id, drivers[9]["driver"].id) == (9, 10)
    # Put 10 online first. As strings "10" sorts before "9", so only the tie-break can pick 9.
    for who in (drivers[9], drivers[8]):
        await put_online(who, north(800), PICKUP_LNG)

    ride = await request_ride(rider)

    assert await offers_of(db, ride["id"]) == [(9, "PENDING")]


async def test_a_driver_is_available_again_after_a_completed_ride(
    client, db, make_user, online_at, request_ride, accept_offer
):
    driver = await online_at(500)
    first_rider = await make_user("rider")
    second_rider = await make_user("rider")
    first = await request_ride(first_rider)
    await accept_offer(driver)
    for step in ("arrive", "start", "complete"):
        assert (await client.post(f"/rides/{first['id']}/{step}", headers=driver["headers"])).status_code == 200

    second = await request_ride(second_rider)

    assert second["status"] == "REQUESTED"
    assert await offers_of(db, second["id"]) == [(driver["driver"].id, "PENDING")]


async def test_a_driver_is_available_again_after_the_rider_cancels(
    client, db, make_user, online_at, request_ride, accept_offer
):
    driver = await online_at(500)
    first_rider = await make_user("rider")
    second_rider = await make_user("rider")
    first = await request_ride(first_rider)
    await accept_offer(driver)
    assert (await client.post(f"/rides/{first['id']}/cancel", headers=first_rider["headers"])).status_code == 200

    second = await request_ride(second_rider)

    assert second["status"] == "REQUESTED"
    assert await offers_of(db, second["id"]) == [(driver["driver"].id, "PENDING")]


async def test_a_redis_failure_leaves_no_ride_behind(client, db, rider, online_at, monkeypatch):
    await online_at(500)

    async def broken_search(*args):
        raise RedisConnectionError("redis is down")

    monkeypatch.setattr(drivers_repo, "search_nearby", broken_search)

    response = await client.post("/rides", json=RIDE_BODY, headers=rider["headers"])

    assert response.status_code == 503
    assert await db.scalar(select(func.count()).select_from(Ride)) == 0
    assert await db.scalar(select(func.count()).select_from(RideEvent)) == 0
    assert await db.scalar(select(func.count()).select_from(RideOffer)) == 0


async def test_a_rider_with_an_active_ride_gets_409_and_matching_is_not_called(
    client, rider, insert_ride, monkeypatch
):
    await insert_ride(rider, RideStatus.REQUESTED)

    async def must_not_run(*args):
        pytest.fail("matching ran for a rider who already has an active ride")

    monkeypatch.setattr(matching, "offer_to_next_driver", must_not_run)

    response = await client.post("/rides", json=RIDE_BODY, headers=rider["headers"])

    assert response.status_code == 409


async def test_only_riders_can_request_and_the_assign_route_is_gone(client, driver, admin):
    assert (await client.post("/rides", json=RIDE_BODY, headers=driver["headers"])).status_code == 403
    assert (await client.post("/rides", json=RIDE_BODY, headers=admin["headers"])).status_code == 403
    assert (await client.post("/rides", json=RIDE_BODY)).status_code == 401

    response = await client.post("/admin/rides/1/assign", json={"driver_id": 1}, headers=admin["headers"])

    assert response.status_code in (404, 405)
