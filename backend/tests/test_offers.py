import asyncio
import time
from datetime import datetime, timedelta, timezone

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import RedisError
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError

from app.database import redis_client
from app.models import OfferStatus, Ride, RideEvent, RideOffer, RideStatus, VerificationStatus
from app.repositories import drivers as drivers_repo
from app.repositories import events
from app.services import matching
from app.services import offers as offers_service
from test_matching import offers_of, online_at, request_ride  # noqa: F401  (the last two are fixtures)
from test_rides import RIDE_BODY
from test_websocket import assert_silent, open_socket, receive  # noqa: F401  (open_socket is a fixture)

OFFER_FIELDS = {
    "id", "ride_id", "pickup_address", "pickup_lat", "pickup_lng", "dropoff_address", "dropoff_lat", "dropoff_lng",
    "trip_distance_m", "trip_duration_s", "fare_estimate", "pickup_distance_m", "expires_in",
}


async def offer_of(client, who: dict) -> dict:
    response = await client.get("/drivers/me/offer", headers=who["headers"])
    assert response.status_code == 200, response.text
    return response.json()


async def make_overdue(db, ride_id: int) -> None:
    """Moves the deadline of the ride's pending offer into the past, which is what 15 seconds of waiting does."""
    await db.execute(
        update(RideOffer)
        .where(RideOffer.ride_id == ride_id, RideOffer.status == OfferStatus.PENDING)
        .values(expires_at=datetime.now(timezone.utc) - timedelta(seconds=1))
    )
    await db.commit()


async def ride_status(db, ride_id: int) -> str:
    return (await db.scalar(select(Ride.status).where(Ride.id == ride_id))).value


async def ride_events(db, ride_id: int) -> list[tuple]:
    result = await db.execute(
        select(RideEvent.from_status, RideEvent.to_status, RideEvent.actor_user_id).where(RideEvent.ride_id == ride_id).order_by(RideEvent.id)
    )
    return [(from_status.value if from_status else None, to_status.value, actor) for from_status, to_status, actor in result.all()]


async def count(db, model) -> int:
    return await db.scalar(select(func.count()).select_from(model))


# --- creating the offer ---


async def test_a_request_makes_one_pending_offer_and_the_ride_stays_requested(client, db, rider, online_at, request_ride):
    driver = await online_at(500)

    ride = await request_ride(rider)

    assert ride["status"] == "REQUESTED"
    assert ride["driver_id"] is None
    rows = (await db.execute(select(RideOffer.driver_id, RideOffer.status, RideOffer.pickup_distance_m, RideOffer.expires_at, RideOffer.responded_at))).all()
    assert len(rows) == 1
    driver_id, status, pickup_distance_m, expires_at, responded_at = rows[0]
    assert (driver_id, status, responded_at) == (driver["driver"].id, OfferStatus.PENDING, None)
    assert abs(pickup_distance_m - 500) < 20
    assert 10 < (expires_at - datetime.now(timezone.utc)).total_seconds() < 16
    assert await ride_events(db, ride["id"]) == [(None, "REQUESTED", rider["user"].id)]
    assert (await client.get("/rides/active", headers=rider["headers"])).status_code == 200
    assert (await client.get("/rides/active", headers=driver["headers"])).status_code == 404


async def test_the_drivers_offer_endpoint(client, rider, admin, online_at, request_ride):
    driver = await online_at(500)
    elsewhere = await online_at(1500)
    ride = await request_ride(rider)

    response = await client.get("/drivers/me/offer", headers=driver["headers"])

    assert response.status_code == 200
    offer = response.json()
    assert set(offer) == OFFER_FIELDS
    assert offer["ride_id"] == ride["id"]
    assert (offer["pickup_address"], offer["dropoff_address"]) == (RIDE_BODY["pickup_address"], RIDE_BODY["dropoff_address"])
    assert (offer["pickup_lat"], offer["pickup_lng"]) == (RIDE_BODY["pickup_lat"], RIDE_BODY["pickup_lng"])
    assert (offer["dropoff_lat"], offer["dropoff_lng"]) == (RIDE_BODY["dropoff_lat"], RIDE_BODY["dropoff_lng"])
    assert (offer["trip_distance_m"], offer["trip_duration_s"]) == (5000, 900)  # the fake route
    assert offer["fare_estimate"] == ride["fare_estimate"]
    assert abs(offer["pickup_distance_m"] - 500) < 20
    assert 0 < offer["expires_in"] <= 15
    assert offer["expires_in"] == round(offer["expires_in"], 1)
    for private in (rider["user"].email, rider["user"].name, "rider_id"):
        assert private not in response.text

    assert (await client.get("/drivers/me/offer", headers=elsewhere["headers"])).status_code == 404
    assert (await client.get("/drivers/me/offer", headers=rider["headers"])).status_code == 403
    assert (await client.get("/drivers/me/offer", headers=admin["headers"])).status_code == 403
    assert (await client.get("/drivers/me/offer")).status_code == 401


async def test_only_the_offered_driver_gets_offer_created(client, open_socket, rider, online_at, request_ride):
    offered = await online_at(500)
    other = await online_at(1500)
    offered_ws = await open_socket(offered)
    other_ws = await open_socket(other)
    rider_ws = await open_socket(rider)

    ride = await request_ride(rider)

    event = await receive(offered_ws)
    offer = await offer_of(client, offered)
    assert event == {"type": "offer_created", "data": {"offer_id": offer["id"], "ride_id": ride["id"]}}
    await assert_silent(offered_ws)  # exactly one
    await assert_silent(other_ws)
    await assert_silent(rider_ws)


# --- answering ---


async def test_accept_assigns_the_ride_and_tells_both_sides(client, db, open_socket, rider, online_at, request_ride):
    driver = await online_at(500)
    driver_ws = await open_socket(driver)
    rider_ws = await open_socket(rider)
    ride = await request_ride(rider)
    offer = await offer_of(client, driver)
    assert (await receive(driver_ws))["type"] == "offer_created"

    response = await client.post(f"/offers/{offer['id']}/accept", headers=driver["headers"])

    assert response.status_code == 200
    body = response.json()
    assert body["id"] == ride["id"]
    assert body["status"] == "DRIVER_ASSIGNED"
    assert body["driver_id"] == driver["driver"].id
    row = (await db.execute(select(RideOffer.status, RideOffer.responded_at).where(RideOffer.id == offer["id"]))).one()
    assert row.status == OfferStatus.ACCEPTED
    assert row.responded_at is not None
    assert await ride_events(db, ride["id"]) == [
        (None, "REQUESTED", rider["user"].id),
        ("REQUESTED", "DRIVER_ASSIGNED", driver["user"].id),
    ]
    assert await receive(rider_ws) == {"type": "ride_updated", "data": {"ride_id": ride["id"], "status": "DRIVER_ASSIGNED"}}
    assert await receive(driver_ws) == {
        "type": "offer_closed",
        "data": {"offer_id": offer["id"], "ride_id": ride["id"], "reason": "accepted"},
    }
    active = await client.get("/rides/active", headers=driver["headers"])
    assert active.status_code == 200
    assert active.json()["id"] == ride["id"]
    assert (await client.get("/drivers/me/offer", headers=driver["headers"])).status_code == 404


async def test_reject_offers_the_ride_to_the_next_driver_and_never_back(
    client, db, open_socket, rider, online_at, request_ride, accept_offer
):
    first = await online_at(500)
    second = await online_at(1200)
    first_ws = await open_socket(first)
    second_ws = await open_socket(second)
    rider_ws = await open_socket(rider)
    ride = await request_ride(rider)
    first_offer = await offer_of(client, first)
    assert (await receive(first_ws))["type"] == "offer_created"

    response = await client.post(f"/offers/{first_offer['id']}/reject", headers=first["headers"])

    assert response.status_code == 204
    assert response.content == b""
    assert await offers_of(db, ride["id"]) == [(first["driver"].id, "REJECTED"), (second["driver"].id, "PENDING")]
    responded_at = await db.scalar(select(RideOffer.responded_at).where(RideOffer.id == first_offer["id"]))
    assert responded_at is not None
    assert await receive(first_ws) == {
        "type": "offer_closed",
        "data": {"offer_id": first_offer["id"], "ride_id": ride["id"], "reason": "rejected"},
    }
    second_offer = await offer_of(client, second)
    assert await receive(second_ws) == {"type": "offer_created", "data": {"offer_id": second_offer["id"], "ride_id": ride["id"]}}
    await assert_silent(rider_ws)
    assert await ride_status(db, ride["id"]) == "REQUESTED"
    assert await ride_events(db, ride["id"]) == [(None, "REQUESTED", rider["user"].id)]
    assert (await client.get("/drivers/me/offer", headers=first["headers"])).status_code == 404

    accepted = await accept_offer(second)

    assert accepted["driver_id"] == second["driver"].id
    assert await offers_of(db, ride["id"]) == [(first["driver"].id, "REJECTED"), (second["driver"].id, "ACCEPTED")]


async def test_reject_with_nobody_else_ends_the_ride(client, db, open_socket, rider, online_at, request_ride):
    driver = await online_at(500)
    rider_ws = await open_socket(rider)
    ride = await request_ride(rider)
    offer = await offer_of(client, driver)

    response = await client.post(f"/offers/{offer['id']}/reject", headers=driver["headers"])

    assert response.status_code == 204
    # The driver who just rejected is the only one, and a ride is never offered to the same driver twice.
    assert await offers_of(db, ride["id"]) == [(driver["driver"].id, "REJECTED")]
    assert await ride_events(db, ride["id"]) == [(None, "REQUESTED", rider["user"].id), ("REQUESTED", "NO_DRIVER_FOUND", None)]
    assert await receive(rider_ws) == {"type": "ride_updated", "data": {"ride_id": ride["id"], "status": "NO_DRIVER_FOUND"}}
    assert (await client.get("/rides/active", headers=rider["headers"])).status_code == 404
    again = await client.post("/rides", json=RIDE_BODY, headers=rider["headers"])
    assert again.status_code == 201
    assert again.json()["status"] == "REQUESTED"  # a new ride, so the driver is offered it


# --- expiry ---


async def test_an_expired_offer_moves_on_to_the_next_driver(client, db, open_socket, rider, online_at, request_ride):
    first = await online_at(500)
    second = await online_at(1200)
    first_ws = await open_socket(first)
    second_ws = await open_socket(second)
    ride = await request_ride(rider)
    first_offer = await offer_of(client, first)
    await receive(first_ws)
    await make_overdue(db, ride["id"])

    # The deadline is hard even before the sweep: the offer is already gone for the driver.
    assert (await client.get("/drivers/me/offer", headers=first["headers"])).status_code == 404

    await offers_service.expire_due_offers()

    assert await offers_of(db, ride["id"]) == [(first["driver"].id, "EXPIRED"), (second["driver"].id, "PENDING")]
    assert await db.scalar(select(RideOffer.responded_at).where(RideOffer.id == first_offer["id"])) is None
    assert await receive(first_ws) == {
        "type": "offer_closed",
        "data": {"offer_id": first_offer["id"], "ride_id": ride["id"], "reason": "expired"},
    }
    second_offer = await offer_of(client, second)
    assert await receive(second_ws) == {"type": "offer_created", "data": {"offer_id": second_offer["id"], "ride_id": ride["id"]}}
    assert await ride_status(db, ride["id"]) == "REQUESTED"


async def test_an_expired_offer_with_nobody_left_ends_the_ride(client, db, open_socket, rider, online_at, request_ride):
    driver = await online_at(500)
    rider_ws = await open_socket(rider)
    ride = await request_ride(rider)
    await make_overdue(db, ride["id"])

    await offers_service.expire_due_offers()

    assert await offers_of(db, ride["id"]) == [(driver["driver"].id, "EXPIRED")]
    assert await ride_events(db, ride["id"]) == [(None, "REQUESTED", rider["user"].id), ("REQUESTED", "NO_DRIVER_FOUND", None)]
    assert await receive(rider_ws) == {"type": "ride_updated", "data": {"ride_id": ride["id"], "status": "NO_DRIVER_FOUND"}}

    # Nothing is due any more, so a second pass changes nothing.
    await offers_service.expire_due_offers()
    assert await offers_of(db, ride["id"]) == [(driver["driver"].id, "EXPIRED")]


@pytest.mark.parametrize("action", ["accept", "reject"])
async def test_answering_after_the_deadline_is_409_even_before_the_sweep(client, db, rider, online_at, request_ride, action):
    driver = await online_at(500)
    ride = await request_ride(rider)
    offer = await offer_of(client, driver)
    await make_overdue(db, ride["id"])

    response = await client.post(f"/offers/{offer['id']}/{action}", headers=driver["headers"])

    assert response.status_code == 409
    assert response.json()["detail"] == "This offer has expired"
    assert await ride_status(db, ride["id"]) == "REQUESTED"
    assert await offers_of(db, ride["id"]) == [(driver["driver"].id, "PENDING")]

    await offers_service.expire_due_offers()  # the sweeper still handles it

    assert await offers_of(db, ride["id"]) == [(driver["driver"].id, "EXPIRED")]
    assert await ride_status(db, ride["id"]) == "NO_DRIVER_FOUND"


async def test_the_real_sweeper_ends_an_unanswered_ride_on_its_own(live_server, db, rider, online_at, request_ride, monkeypatch):
    monkeypatch.setattr(matching, "OFFER_TIMEOUT_SECONDS", 1)
    driver = await online_at(500)
    ride = await request_ride(rider)

    # No other call is made: only the background task can do this.
    started = time.monotonic()
    while await ride_status(db, ride["id"]) != "NO_DRIVER_FOUND":
        assert time.monotonic() - started < 4, "the sweeper did not end the ride"
        await asyncio.sleep(0.1)
    assert await offers_of(db, ride["id"]) == [(driver["driver"].id, "EXPIRED")]


# --- who may answer, and in which state ---


async def test_who_may_answer_an_offer(client, db, admin, rider, online_at, request_ride):
    driver = await online_at(500)
    other = await online_at(1500)
    ride = await request_ride(rider)
    offer_id = (await offer_of(client, driver))["id"]

    for action in ("accept", "reject"):
        url = f"/offers/{offer_id}/{action}"
        assert (await client.post(url, headers=other["headers"])).status_code == 404  # someone else's offer
        assert (await client.post(url, headers=rider["headers"])).status_code == 403
        assert (await client.post(url, headers=admin["headers"])).status_code == 403
        assert (await client.post(url)).status_code == 401
        assert (await client.post(f"/offers/999999/{action}", headers=driver["headers"])).status_code == 404

    assert await offers_of(db, ride["id"]) == [(driver["driver"].id, "PENDING")]  # nothing changed


async def test_an_offer_can_be_answered_only_once(client, db, make_user, online_at, request_ride):
    driver = await online_at(500)
    first = await request_ride(await make_user("rider"))
    offer_id = (await offer_of(client, driver))["id"]

    assert (await client.post(f"/offers/{offer_id}/accept", headers=driver["headers"])).status_code == 200
    again = await client.post(f"/offers/{offer_id}/accept", headers=driver["headers"])
    assert again.status_code == 409
    assert again.json()["detail"] == "This offer is no longer available"
    assert (await client.post(f"/offers/{offer_id}/reject", headers=driver["headers"])).status_code == 409
    assert await ride_status(db, first["id"]) == "DRIVER_ASSIGNED"

    # accept after reject
    second = await online_at(500)
    ride = await request_ride(await make_user("rider"))
    rejected_offer_id = (await offer_of(client, second))["id"]
    assert (await client.post(f"/offers/{rejected_offer_id}/reject", headers=second["headers"])).status_code == 204
    assert (await client.post(f"/offers/{rejected_offer_id}/accept", headers=second["headers"])).status_code == 409
    assert (await client.post(f"/offers/{rejected_offer_id}/reject", headers=second["headers"])).status_code == 409
    assert await ride_status(db, ride["id"]) == "NO_DRIVER_FOUND"


async def test_the_rider_cancelling_closes_the_pending_offer(client, db, open_socket, rider, online_at, request_ride):
    driver = await online_at(500)
    driver_ws = await open_socket(driver)
    ride = await request_ride(rider)
    offer = await offer_of(client, driver)
    await receive(driver_ws)

    response = await client.post(f"/rides/{ride['id']}/cancel", headers=rider["headers"])

    assert response.status_code == 200
    assert response.json()["status"] == "CANCELLED"
    assert await offers_of(db, ride["id"]) == [(driver["driver"].id, "CANCELLED")]
    assert await receive(driver_ws) == {
        "type": "offer_closed",
        "data": {"offer_id": offer["id"], "ride_id": ride["id"], "reason": "ride_cancelled"},
    }
    assert (await client.get("/drivers/me/offer", headers=driver["headers"])).status_code == 404
    answer = await client.post(f"/offers/{offer['id']}/accept", headers=driver["headers"])
    assert answer.status_code == 409
    assert await ride_status(db, ride["id"]) == "CANCELLED"

    # The driver is free again: the next ride is offered to the same driver.
    next_ride = await request_ride(rider)
    assert await offers_of(db, next_ride["id"]) == [(driver["driver"].id, "PENDING")]


async def test_cancelling_after_the_accept_has_no_offer_to_close(client, db, open_socket, rider, driver, assign_ride):
    ride = await assign_ride(rider, driver)
    driver_ws = await open_socket(driver)

    response = await client.post(f"/rides/{ride['id']}/cancel", headers=rider["headers"])

    assert response.status_code == 200
    assert await offers_of(db, ride["id"]) == [(driver["driver"].id, "ACCEPTED")]
    # No offer_closed (there is no offer to close). The only message is the news that the ride is cancelled.
    assert await receive(driver_ws) == {"type": "ride_updated", "data": {"ride_id": ride["id"], "status": "CANCELLED"}}
    await assert_silent(driver_ws)


# --- limits ---


async def test_at_most_max_offers_per_ride(client, db, rider, online_at, request_ride, monkeypatch):
    monkeypatch.setattr(matching, "MAX_OFFERS_PER_RIDE", 2)
    drivers = [await online_at(meters) for meters in (500, 1000, 1500)]
    ride = await request_ride(rider)

    for who in drivers[:2]:
        offer = await offer_of(client, who)
        assert (await client.post(f"/offers/{offer['id']}/reject", headers=who["headers"])).status_code == 204

    assert await ride_status(db, ride["id"]) == "NO_DRIVER_FOUND"
    assert await offers_of(db, ride["id"]) == [(drivers[0]["driver"].id, "REJECTED"), (drivers[1]["driver"].id, "REJECTED")]
    assert (await client.get("/drivers/me/offer", headers=drivers[2]["headers"])).status_code == 404


async def test_a_driver_with_a_pending_offer_is_not_offered_a_second_ride(client, db, make_user, rider, online_at, request_ride):
    driver = await online_at(500)
    first = await request_ride(rider)

    second = await request_ride(await make_user("rider"))

    assert second["status"] == "NO_DRIVER_FOUND"
    assert await offers_of(db, second["id"]) == []
    assert await offers_of(db, first["id"]) == [(driver["driver"].id, "PENDING")]


# --- refusals ---


async def test_accept_needs_the_driver_to_be_online(client, db, rider, online_at, request_ride):
    driver = await online_at(500)
    ride = await request_ride(rider)
    offer = await offer_of(client, driver)
    await redis_client.delete(f"driver:{driver['driver'].id}:presence")

    response = await client.post(f"/offers/{offer['id']}/accept", headers=driver["headers"])

    assert response.status_code == 409
    assert "offline" in response.json()["detail"]
    assert await ride_status(db, ride["id"]) == "REQUESTED"
    assert await offers_of(db, ride["id"]) == [(driver["driver"].id, "PENDING")]


@pytest.mark.parametrize("status", [VerificationStatus.pending, VerificationStatus.rejected])
async def test_accept_needs_the_driver_to_be_approved(client, db, rider, online_at, request_ride, status):
    driver = await online_at(500)
    ride = await request_ride(rider)
    offer = await offer_of(client, driver)
    driver["driver"].verification_status = status
    await db.commit()

    response = await client.post(f"/offers/{offer['id']}/accept", headers=driver["headers"])

    assert response.status_code == 403
    assert await ride_status(db, ride["id"]) == "REQUESTED"
    assert await offers_of(db, ride["id"]) == [(driver["driver"].id, "PENDING")]


# --- failures ---


async def test_a_redis_failure_while_matching_leaves_no_rows(client, db, rider, online_at, monkeypatch):
    await online_at(500)

    async def broken_search(*args):
        raise RedisConnectionError("redis is down")

    monkeypatch.setattr(drivers_repo, "search_nearby", broken_search)

    response = await client.post("/rides", json=RIDE_BODY, headers=rider["headers"])

    assert response.status_code == 503
    for model in (Ride, RideEvent, RideOffer):
        assert await count(db, model) == 0


async def test_a_failed_publish_does_not_fail_or_undo_the_request(client, db, rider, online_at, monkeypatch):
    driver = await online_at(500)

    async def broken_publish(*args):
        raise RedisError("publish failed")

    monkeypatch.setattr(redis_client, "publish", broken_publish)

    created = await client.post("/rides", json=RIDE_BODY, headers=rider["headers"])

    assert created.status_code == 201
    assert await offers_of(db, created.json()["id"]) == [(driver["driver"].id, "PENDING")]
    offer = await offer_of(client, driver)
    accepted = await client.post(f"/offers/{offer['id']}/accept", headers=driver["headers"])
    assert accepted.status_code == 200
    assert accepted.json()["status"] == "DRIVER_ASSIGNED"


async def test_events_are_published_only_after_the_commit(client, db, test_engine, rider, online_at, request_ride, monkeypatch):
    first = await online_at(500)
    second = await online_at(1200)
    seen = []
    real_publish = events.publish

    async def spy(user_id, type, data):
        # A new connection, so only committed data is visible to it.
        async with test_engine.connect() as connection:
            offers = (await connection.execute(select(RideOffer.id, RideOffer.status))).all()
            rides = (await connection.execute(select(Ride.id, Ride.status))).all()
        seen.append((type, data, [(o.id, o.status.value) for o in offers], [(r.id, r.status.value) for r in rides]))
        return await real_publish(user_id, type, data)

    monkeypatch.setattr(events, "publish", spy)

    ride = await request_ride(rider)
    first_offer = await offer_of(client, first)
    await client.post(f"/offers/{first_offer['id']}/reject", headers=first["headers"])
    second_offer = await offer_of(client, second)
    await client.post(f"/offers/{second_offer['id']}/accept", headers=second["headers"])

    by_type = [(type, data.get("offer_id"), offers, rides) for type, data, offers, rides in seen]
    assert [type for type, *_ in by_type] == ["offer_created", "offer_closed", "offer_created", "ride_updated", "offer_closed"]
    ride_id = ride["id"]
    # offer_created already finds its offer; ride_updated already finds the new ride status; offer_closed the new offer status.
    assert (first_offer["id"], "PENDING") in by_type[0][2]
    assert (first_offer["id"], "REJECTED") in by_type[1][2]
    assert (second_offer["id"], "PENDING") in by_type[2][2]
    assert (ride_id, "DRIVER_ASSIGNED") in by_type[3][3]
    assert (second_offer["id"], "ACCEPTED") in by_type[4][2]


async def test_offer_constraints(db, rider, make_user, insert_ride):
    # Ids are read up front: a rollback expires everything the session holds.
    ride_id = (await insert_ride(rider, RideStatus.REQUESTED)).id
    first_id, second_id = (await make_user("driver"))["driver"].id, (await make_user("driver"))["driver"].id
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=15)

    def offer(driver_id, status):
        return RideOffer(ride_id=ride_id, driver_id=driver_id, status=status, pickup_distance_m=100, expires_at=expires_at)

    db.add(offer(first_id, OfferStatus.PENDING))
    await db.commit()

    db.add(offer(second_id, OfferStatus.PENDING))  # a second open offer for the same ride
    with pytest.raises(IntegrityError, match="uq_ride_offers_one_pending_per_ride"):
        await db.commit()
    await db.rollback()

    db.add(offer(first_id, OfferStatus.REJECTED))  # the same driver again, whatever the status
    with pytest.raises(IntegrityError, match="uq_ride_offers_ride_id_driver_id"):
        await db.commit()
    await db.rollback()

    db.add(offer(second_id, OfferStatus.EXPIRED))  # a finished offer to another driver is fine
    await db.commit()


# --- concurrency (basic: the double-booking races belong to M4) ---


async def test_two_simultaneous_accepts_give_one_winner(client, db, rider, online_at, request_ride):
    driver = await online_at(500)
    ride = await request_ride(rider)
    offer = await offer_of(client, driver)

    responses = await asyncio.gather(
        client.post(f"/offers/{offer['id']}/accept", headers=driver["headers"]),
        client.post(f"/offers/{offer['id']}/accept", headers=driver["headers"]),
    )

    assert sorted(response.status_code for response in responses) == [200, 409]
    assert await ride_events(db, ride["id"]) == [(None, "REQUESTED", rider["user"].id), ("REQUESTED", "DRIVER_ASSIGNED", driver["user"].id)]


async def test_two_simultaneous_rejects_make_exactly_one_new_offer(client, db, rider, online_at, request_ride):
    first = await online_at(500)
    second = await online_at(1200)
    await online_at(1800)
    ride = await request_ride(rider)
    offer = await offer_of(client, first)

    responses = await asyncio.gather(
        client.post(f"/offers/{offer['id']}/reject", headers=first["headers"]),
        client.post(f"/offers/{offer['id']}/reject", headers=first["headers"]),
    )

    assert sorted(response.status_code for response in responses) == [204, 409]
    rows = await offers_of(db, ride["id"])
    assert rows == [(first["driver"].id, "REJECTED"), (second["driver"].id, "PENDING")]
