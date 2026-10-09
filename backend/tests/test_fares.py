import asyncio
import json
import math

import pytest
import pytest_asyncio
from redis.exceptions import RedisError
from sqlalchemy import func, select, text

from app.config import settings
from app.database import redis_client
from app.models import PricingRule, Ride, RideEvent, RideStatus
from app.repositories import rides as rides_repo
from test_edge_cases import logged  # noqa: F401  (a fixture: caplog attached to uvicorn's logger)
from test_rides import RIDE_BODY
from test_websocket import open_socket  # noqa: F401  (a fixture)

LAT = RIDE_BODY["pickup_lat"]
LNG = RIDE_BODY["pickup_lng"]
METERS_PER_DEGREE = math.radians(1) * 6371000  # the haversine radius of the app, so expected distances match exactly
FAR_DEGREES = 5000 / METERS_PER_DEGREE  # a 5 km move

# Written out by hand, not imported from the app, so a wrong change to the key fails the tests.
TRIP_KEY = "ride:{}:trip"

TRIP_KEYS = {
    "kind", "distance_m", "duration_s", "distance_source", "fallback_reason", "tracked_pings", "jumps_ignored", "base_fare",
    "distance_fare", "time_fare", "minimum_fare_applied", "computed_fare", "fare_cap", "capped",
}


@pytest_asyncio.fixture
async def trip(rider, driver, assign_ride):
    """A rider and a driver with a DRIVER_ASSIGNED ride (the fake route: 5000 m, 900 s, estimate 14000)."""
    ride = await assign_ride(rider, driver)
    return {"rider": rider, "driver": driver, "id": ride["id"]}


async def go(client, trip: dict, *names: str) -> None:
    """Drives the ride through the given steps (arrive, start, complete) as its driver."""
    for name in names:
        body = {"otp": "1234"} if name == "start" else None
        response = await client.post(f"/rides/{trip['id']}/{name}", json=body, headers=trip["driver"]["headers"])
        assert response.status_code == 200, response.text


async def ping(client, who: dict, north_m: float = 0, lat_degrees: float = 0):
    """A location ping `north_m` meters north of the pickup (plus an extra `lat_degrees`)."""
    lat = LAT + north_m / METERS_PER_DEGREE + lat_degrees
    response = await client.post("/drivers/me/location", json={"lat": lat, "lng": LNG}, headers=who["headers"])
    assert response.status_code == 200, response.text


async def meter(ride_id: int) -> dict | None:
    fields = await redis_client.hgetall(TRIP_KEY.format(ride_id))
    return {name: float(value) for name, value in fields.items()} if fields else None


async def row(db, ride_id: int):
    """The stored fare columns and status of a ride. Columns only, so the session's identity map cannot show an older copy."""
    result = await db.execute(
        select(Ride.status, Ride.final_fare, Ride.actual_distance_m, Ride.actual_duration_s, Ride.fare_breakdown).where(Ride.id == ride_id)
    )
    return result.one()


async def event_count(db, ride_id: int) -> int:
    return await db.scalar(select(func.count()).select_from(RideEvent).where(RideEvent.ride_id == ride_id))


async def set_times(db, ride_id: int, started_seconds_ago: int) -> None:
    await db.execute(text("UPDATE rides SET started_at = now() - make_interval(secs => :s) WHERE id = :id"), {"s": started_seconds_ago, "id": ride_id})
    await db.commit()


async def age_assignment(db, ride_id: int, seconds: int) -> None:
    await db.execute(
        text("UPDATE ride_events SET created_at = now() - make_interval(secs => :s) WHERE ride_id = :id AND to_status = 'DRIVER_ASSIGNED'"),
        {"s": seconds, "id": ride_id},
    )
    await db.commit()


async def complete(client, trip: dict):
    return await client.post(f"/rides/{trip['id']}/complete", headers=trip["driver"]["headers"])


async def walk(client, fake_clock, trip: dict, steps_m: list[float], step_seconds: float = 3) -> None:
    """Pings at the given distances north of the pickup, `step_seconds` of fake time apart."""
    for index, north_m in enumerate(steps_m):
        if index:
            fake_clock.advance(step_seconds)
        await ping(client, trip["driver"], north_m)


# The seed rule written out by hand: base 5000, 1200 per km, 200 per minute, minimum 8000 (paise), half-up rounding.
def hand_fare(distance_m: int, duration_s: int) -> int:
    return max(5000 + (1200 * distance_m + 500) // 1000 + (200 * duration_s + 30) // 60, 8000)


# --- A. tracking and the final fare ---


async def test_a_tracked_trip_is_billed_by_its_measured_distance_and_time(client, db, fake_clock, trip):
    await go(client, trip, "arrive", "start")
    await walk(client, fake_clock, trip, [25 * i for i in range(40)])

    stored = await meter(trip["id"])
    assert stored["distance_m"] == pytest.approx(975, abs=1)
    assert stored["pings"] == 40
    assert stored["jumps"] == 0
    assert 86300 < await redis_client.ttl(TRIP_KEY.format(trip["id"])) <= 86400

    await set_times(db, trip["id"], 600)
    response = await complete(client, trip)

    assert response.status_code == 200
    ride = response.json()
    assert ride["actual_distance_m"] == pytest.approx(975, abs=1)
    assert 600 <= ride["actual_duration_s"] <= 602
    assert ride["fare_breakdown"]["distance_source"] == "tracked"
    assert ride["final_fare"] == hand_fare(ride["actual_distance_m"], ride["actual_duration_s"])
    assert isinstance(ride["final_fare"], int)

    breakdown = ride["fare_breakdown"]
    assert set(breakdown) == TRIP_KEYS
    for name, value in breakdown.items():
        assert value is None or isinstance(value, (int, bool, str)), name
    assert breakdown["kind"] == "trip"
    assert breakdown["fallback_reason"] is None
    assert breakdown["tracked_pings"] == 40 and breakdown["jumps_ignored"] == 0
    assert breakdown["base_fare"] == 5000
    assert breakdown["distance_fare"] + breakdown["time_fare"] + 5000 == breakdown["computed_fare"] == ride["final_fare"]
    assert breakdown["fare_cap"] == 21000 and breakdown["capped"] is False
    # What the response says is what is stored.
    stored_row = await row(db, trip["id"])
    assert (stored_row.final_fare, stored_row.actual_distance_m, stored_row.actual_duration_s) == (
        ride["final_fare"], ride["actual_distance_m"], ride["actual_duration_s"]
    )
    assert stored_row.fare_breakdown == breakdown


async def test_pings_outside_a_trip_in_progress_are_not_metered(client, fake_clock, make_user, put_online, trip):
    await ping(client, trip["driver"], 100)  # DRIVER_ASSIGNED
    await go(client, trip, "arrive")
    fake_clock.advance(3)
    await ping(client, trip["driver"], 200)  # DRIVER_ARRIVED
    other = await make_user("driver")
    await put_online(other, LAT, LNG)
    await ping(client, other)  # a driver with no ride
    assert await redis_client.keys("ride:*") == []

    await go(client, trip, "start")
    fake_clock.advance(3)
    await ping(client, trip["driver"], 300)
    assert await redis_client.keys("ride:*") == [TRIP_KEY.format(trip["id"])]
    assert (await meter(trip["id"]))["pings"] == 1

    await go(client, trip, "complete")
    fake_clock.advance(3)
    await ping(client, trip["driver"], 400)  # the ride is over
    assert (await meter(trip["id"]))["pings"] == 1


async def test_a_ping_too_soon_is_ignored_and_a_jump_adds_no_distance(client, fake_clock, trip):
    await go(client, trip, "arrive", "start")
    await ping(client, trip["driver"], 0)
    fake_clock.advance(0.5)
    await ping(client, trip["driver"], 25)  # half a second later: ignored, nothing written
    assert (await meter(trip["id"]))["pings"] == 1
    assert (await meter(trip["id"]))["lat"] == pytest.approx(LAT)

    fake_clock.advance(0.5)  # exactly the minimum gap since the first ping: counted
    await ping(client, trip["driver"], 25)
    assert (await meter(trip["id"]))["pings"] == 2
    assert (await meter(trip["id"]))["distance_m"] == pytest.approx(25, abs=0.01)

    fake_clock.advance(3)
    await ping(client, trip["driver"], 25, lat_degrees=FAR_DEGREES)  # 5 km in 3 s: a jump
    jumped = await meter(trip["id"])
    assert (jumped["jumps"], jumped["pings"]) == (1, 3)
    assert jumped["distance_m"] == pytest.approx(25, abs=0.01)
    assert jumped["lat"] == pytest.approx(LAT + 25 / METERS_PER_DEGREE + FAR_DEGREES)  # the stored point moved to the new position

    fake_clock.advance(3)
    await ping(client, trip["driver"], 50, lat_degrees=FAR_DEGREES)  # 25 m from the new point
    after = await meter(trip["id"])
    assert (after["jumps"], after["pings"]) == (1, 4)
    assert after["distance_m"] == pytest.approx(50, abs=0.01)


@pytest.mark.parametrize("pings", [0, 1])
async def test_without_tracking_the_estimated_distance_is_billed(client, db, fake_clock, trip, pings):
    await go(client, trip, "arrive", "start")
    if pings:
        await ping(client, trip["driver"], 0)
    await set_times(db, trip["id"], 600)

    ride = (await complete(client, trip)).json()

    breakdown = ride["fare_breakdown"]
    assert breakdown["distance_source"] == "estimate"
    assert breakdown["fallback_reason"] == "no_tracking"
    assert breakdown["tracked_pings"] == pings
    assert ride["actual_distance_m"] == ride["distance_m"] == 5000
    assert ride["final_fare"] == hand_fare(5000, ride["actual_duration_s"])


async def test_unreliable_tracking_bills_the_estimate_but_one_glitch_does_not(client, fake_clock, rider, driver, assign_ride):
    # Pings that only teleport: the distance stays near 0, which is far below half of the 5000 m estimate.
    first = {"rider": rider, "driver": driver, "id": (await assign_ride(rider, driver))["id"]}
    await go(client, first, "arrive", "start")
    for index in range(5):
        if index:
            fake_clock.advance(3)
        await ping(client, driver, 0, lat_degrees=FAR_DEGREES * (index % 2))
    assert (await meter(first["id"]))["jumps"] == 4
    ride = (await complete(client, first)).json()
    assert ride["fare_breakdown"]["fallback_reason"] == "unreliable_tracking"
    assert ride["fare_breakdown"]["distance_source"] == "estimate"
    assert ride["fare_breakdown"]["jumps_ignored"] == 4
    assert ride["actual_distance_m"] == 5000

    # One glitch ping in an otherwise full trip (100 pings of 30 m): two jumps, but the distance is still above half the estimate.
    second = {"rider": rider, "driver": driver, "id": (await assign_ride(rider, driver))["id"]}
    await go(client, second, "arrive", "start")
    for index in range(100):
        if index:
            fake_clock.advance(3)
        await ping(client, driver, 30 * index, lat_degrees=FAR_DEGREES if index == 50 else 0)
    assert (await meter(second["id"]))["jumps"] == 2
    ride = (await complete(client, second)).json()
    assert ride["fare_breakdown"]["fallback_reason"] is None
    assert ride["fare_breakdown"]["distance_source"] == "tracked"
    assert ride["actual_distance_m"] == pytest.approx(97 * 30, abs=1)


async def test_a_short_trip_pays_the_minimum_fare_even_below_the_estimate(client, db, fake_clock, trip):
    await go(client, trip, "arrive", "start")
    await walk(client, fake_clock, trip, [30 * i for i in range(11)])  # 300 m
    await set_times(db, trip["id"], 60)

    ride = (await complete(client, trip)).json()

    assert ride["actual_distance_m"] == pytest.approx(300, abs=1)
    assert 5000 + (1200 * ride["actual_distance_m"] + 500) // 1000 + (200 * ride["actual_duration_s"] + 30) // 60 < 8000
    assert ride["final_fare"] == 8000
    assert ride["fare_breakdown"]["minimum_fare_applied"] is True
    assert ride["final_fare"] < ride["fare_estimate"] == 14000


async def test_the_fare_is_capped_at_150_percent_of_the_estimate(client, db, fake_clock, rider, driver, assign_ride):
    # An inflated distance: 150 pings 120 m apart (40 m/s, allowed) back and forth, 17.9 km in all.
    first = {"rider": rider, "driver": driver, "id": (await assign_ride(rider, driver))["id"]}
    await go(client, first, "arrive", "start")
    await walk(client, fake_clock, first, [120 * (index % 2) for index in range(150)])
    await set_times(db, first["id"], 600)
    ride = (await complete(client, first)).json()
    breakdown = ride["fare_breakdown"]
    assert ride["actual_distance_m"] == pytest.approx(149 * 120, abs=1)
    assert breakdown["computed_fare"] > 21000
    assert breakdown["fare_cap"] == 21000
    assert breakdown["capped"] is True
    assert ride["final_fare"] == 21000

    # An inflated time (3 hours), with an estimate whose 150 percent is not whole: the cap rounds down.
    second = {"rider": rider, "driver": driver, "id": (await assign_ride(rider, driver))["id"]}
    await go(client, second, "arrive", "start")
    await walk(client, fake_clock, second, [25 * i for i in range(20)])
    await set_times(db, second["id"], 3 * 3600)
    await db.execute(text("UPDATE rides SET fare_estimate = 14001 WHERE id = :id"), {"id": second["id"]})
    await db.commit()
    ride = (await complete(client, second)).json()
    assert ride["fare_breakdown"]["computed_fare"] > 21001
    assert ride["fare_breakdown"]["fare_cap"] == 21001  # 14001 * 150 // 100
    assert ride["fare_breakdown"]["capped"] is True
    assert ride["final_fare"] == 21001


async def test_redis_failures_never_stop_a_ping_or_a_completion(client, logged, fake_clock, trip, monkeypatch):
    await go(client, trip, "arrive", "start")
    await ping(client, trip["driver"], 0)

    async def broken(*args, **kwargs):
        raise RedisError("down")

    # Saving the point fails: the ping still answers 200 and a warning is logged.
    monkeypatch.setattr(rides_repo, "save_trip", broken)
    fake_clock.advance(3)
    await ping(client, trip["driver"], 25)
    assert any("Could not record a trip point" in record.getMessage() for record in logged.records)
    assert (await meter(trip["id"]))["pings"] == 1
    monkeypatch.undo()

    # Reading the meter fails at completion: 200, billed by the estimate.
    async def broken_read(*args, **kwargs):
        raise RedisError("down")

    monkeypatch.setattr(rides_repo, "get_trip", broken_read)
    logged.clear()
    response = await complete(client, trip)
    assert response.status_code == 200
    assert response.json()["fare_breakdown"]["fallback_reason"] == "no_tracking"
    assert response.json()["fare_breakdown"]["tracked_pings"] == 0
    warnings = [record for record in logged.records if "Could not read the distance meter" in record.getMessage()]
    assert len(warnings) == 1 and warnings[0].exc_info is None


async def test_completing_without_a_pricing_rule_is_503_and_can_be_retried(client, db, trip):
    await go(client, trip, "arrive", "start")
    events_before = await event_count(db, trip["id"])
    rule = (await db.execute(select(PricingRule))).scalar_one()
    values = {column.name: getattr(rule, column.name) for column in PricingRule.__table__.columns}
    await db.execute(text("DELETE FROM pricing_rules"))
    await db.commit()

    response = await complete(client, trip)

    assert response.status_code == 503
    assert response.json()["detail"] == "Pricing is not configured"
    stored = await row(db, trip["id"])
    assert stored.status == RideStatus.IN_PROGRESS and stored.final_fare is None and stored.fare_breakdown is None
    assert await event_count(db, trip["id"]) == events_before

    await db.execute(PricingRule.__table__.insert().values(**values))
    await db.commit()
    retry = await complete(client, trip)
    assert retry.status_code == 200
    assert retry.json()["final_fare"] is not None


async def test_two_simultaneous_completions_settle_once(client, db, trip):
    await go(client, trip, "arrive", "start")
    headers = trip["driver"]["headers"]

    responses = await asyncio.gather(
        client.post(f"/rides/{trip['id']}/complete", headers=headers), client.post(f"/rides/{trip['id']}/complete", headers=headers)
    )

    assert sorted(response.status_code for response in responses) == [200, 409]
    winner = next(response for response in responses if response.status_code == 200).json()
    stored = await row(db, trip["id"])
    assert stored.final_fare == winner["final_fare"] and stored.fare_breakdown == winner["fare_breakdown"]
    completed_events = await db.scalar(
        select(func.count()).select_from(RideEvent).where(RideEvent.ride_id == trip["id"], RideEvent.to_status == RideStatus.COMPLETED)
    )
    assert completed_events == 1


async def test_every_role_sees_the_fare_fields_and_never_the_trip_code(client, db, admin, make_user, insert_ride, trip):
    await go(client, trip, "arrive", "start", "complete")
    for who in (trip["rider"], trip["driver"], admin):
        response = await client.get(f"/rides/{trip['id']}", headers=who["headers"])
        assert response.status_code == 200
        body = response.json()
        assert {"final_fare", "actual_distance_m", "actual_duration_s", "fare_breakdown"} <= set(body)
        assert body["final_fare"] is not None and body["fare_breakdown"]["kind"] == "trip"
        assert "otp" not in body

    # A ride settled before this milestone: the data migration marks it, and it has no fare.
    legacy = await insert_ride(await make_user("rider"), RideStatus.COMPLETED)
    await db.execute(text("""UPDATE rides SET fare_breakdown = '{"kind": "legacy"}'::jsonb WHERE id = :id"""), {"id": legacy.id})
    await db.commit()
    body = (await client.get(f"/rides/{legacy.id}", headers=admin["headers"])).json()
    assert body["final_fare"] is None and body["fare_breakdown"] == {"kind": "legacy"}
    assert body["actual_distance_m"] is None and body["actual_duration_s"] is None


# --- B. cancellation fees ---


async def cancel(client, trip: dict, who: str = "rider"):
    return await client.post(f"/rides/{trip['id']}/cancel", headers=trip[who]["headers"])


async def quote(client, trip: dict, who: str = "rider"):
    return await client.get(f"/rides/{trip['id']}/cancellation-fee", headers=trip[who]["headers"])


async def check_cancelled(db, trip: dict, fee: int, reason: str, by: str = "rider") -> None:
    stored = await row(db, trip["id"])
    assert stored.status == RideStatus.CANCELLED
    assert stored.final_fare == fee
    assert stored.fare_breakdown == {"kind": "cancellation", "fee": fee, "reason": reason, "cancelled_by": by}
    assert stored.actual_distance_m is None and stored.actual_duration_s is None
    last = (await db.execute(select(RideEvent.to_status).where(RideEvent.ride_id == trip["id"]).order_by(RideEvent.id.desc()).limit(1))).scalar_one()
    assert last == RideStatus.CANCELLED


@pytest_asyncio.fixture
async def new_requested(client, make_user, put_online):
    """Returns a function that makes a rider with a REQUESTED ride: a new driver is online with a pending offer."""

    async def create() -> dict:
        who, driver = await make_user("rider"), await make_user("driver")
        await put_online(driver, LAT, LNG)
        created = await client.post("/rides", json=RIDE_BODY, headers=who["headers"])
        assert created.status_code == 201 and created.json()["status"] == "REQUESTED"
        return {"rider": who, "driver": driver, "id": created.json()["id"]}

    return create


async def test_rider_cancelling_before_a_driver_accepts_pays_nothing(client, db, new_requested):
    requested = await new_requested()
    quoted = await quote(client, requested)
    assert quoted.json() == {"fee": 0, "reason": "no_driver_yet"}

    response = await cancel(client, requested)

    assert response.status_code == 200
    assert response.json()["final_fare"] == 0
    await check_cancelled(db, requested, 0, "no_driver_yet")


@pytest.mark.parametrize("age, fee, reason", [(110, 0, "within_free_window"), (130, 3000, "late_cancellation")])
async def test_rider_cancelling_after_the_assignment_pays_by_the_free_window(client, db, trip, age, fee, reason):
    await age_assignment(db, trip["id"], age)
    quoted = await quote(client, trip)
    assert quoted.json() == {"fee": fee, "reason": reason}

    response = await cancel(client, trip)

    assert response.status_code == 200 and response.json()["final_fare"] == fee
    await check_cancelled(db, trip, fee, reason)


async def test_rider_cancelling_after_the_driver_arrived_always_pays(client, db, trip):
    await go(client, trip, "arrive")
    quoted = await quote(client, trip)
    assert quoted.json() == {"fee": 3000, "reason": "driver_arrived"}

    response = await cancel(client, trip)

    assert response.status_code == 200
    await check_cancelled(db, trip, 3000, "driver_arrived")


@pytest.mark.parametrize("arrived", [False, True])
async def test_a_driver_cancelling_never_causes_a_fee(client, db, trip, arrived):
    await age_assignment(db, trip["id"], 500)  # far past the free window: it would cost the rider
    if arrived:
        await go(client, trip, "arrive")

    response = await cancel(client, trip, "driver")

    assert response.status_code == 200 and response.json()["final_fare"] == 0
    await check_cancelled(db, trip, 0, "driver_cancelled", by="driver")


async def test_the_fee_and_window_come_from_the_pricing_rule(client, db, trip):
    await db.execute(text("UPDATE pricing_rules SET free_cancel_seconds = 0, cancellation_fee = 5000"))
    await db.commit()

    assert (await quote(client, trip)).json() == {"fee": 5000, "reason": "late_cancellation"}
    response = await cancel(client, trip)

    assert response.status_code == 200
    await check_cancelled(db, trip, 5000, "late_cancellation")


async def test_a_missing_rule_blocks_only_the_cancels_that_need_it(client, db, new_requested, trip):
    requested = await new_requested()
    await db.execute(text("DELETE FROM pricing_rules"))
    await db.commit()
    events_before = await event_count(db, trip["id"])

    blocked = await cancel(client, trip)  # the rider, DRIVER_ASSIGNED
    assert blocked.status_code == 503 and blocked.json()["detail"] == "Pricing is not configured"
    assert (await quote(client, trip)).status_code == 503
    stored = await row(db, trip["id"])
    assert stored.status == RideStatus.DRIVER_ASSIGNED and stored.final_fare is None
    assert await event_count(db, trip["id"]) == events_before

    assert (await cancel(client, trip, "driver")).status_code == 200  # the driver
    assert (await cancel(client, requested)).status_code == 200  # REQUESTED


async def test_cancelling_a_trip_in_progress_is_refused_and_a_ride_without_a_driver_has_no_fare(client, db, trip, make_user):
    await go(client, trip, "arrive", "start")

    for who in ("rider", "driver"):
        assert (await cancel(client, trip, who)).status_code == 409
    stored = await row(db, trip["id"])
    assert stored.status == RideStatus.IN_PROGRESS and stored.final_fare is None and stored.fare_breakdown is None

    nobody = await make_user("rider")
    created = await client.post("/rides", json=RIDE_BODY, headers=nobody["headers"])  # the only driver is busy
    assert created.json()["status"] == "NO_DRIVER_FOUND"
    stored = await row(db, created.json()["id"])
    assert stored.final_fare is None and stored.fare_breakdown is None and stored.actual_distance_m is None


async def test_the_quote_is_the_same_function_as_the_charge(client, db, new_requested, trip):
    requested = await new_requested()
    # REQUESTED, within the free window, late, and after arrival: the quote equals what the cancel then charges.
    assert (await quote(client, requested)).json() == {"fee": 0, "reason": "no_driver_yet"}
    assert (await cancel(client, requested)).json()["final_fare"] == 0

    assert (await quote(client, trip)).json() == {"fee": 0, "reason": "within_free_window"}
    await age_assignment(db, trip["id"], 130)
    late = (await quote(client, trip)).json()
    assert late == {"fee": 3000, "reason": "late_cancellation"}
    await go(client, trip, "arrive")
    arrived = (await quote(client, trip)).json()
    assert arrived == {"fee": 3000, "reason": "driver_arrived"}
    charged = await cancel(client, trip)
    assert charged.json()["final_fare"] == arrived["fee"]
    assert charged.json()["fare_breakdown"]["reason"] == arrived["reason"]


async def test_who_may_ask_for_the_quote_and_the_quote_changes_nothing(client, db, trip, admin, make_user):
    before = await row(db, trip["id"]), await event_count(db, trip["id"])

    assert (await quote(client, trip)).status_code == 200
    other_rider = await make_user("rider")
    assert (await client.get(f"/rides/{trip['id']}/cancellation-fee", headers=other_rider["headers"])).status_code == 404
    assert (await quote(client, trip, "driver")).status_code == 403
    assert (await client.get(f"/rides/{trip['id']}/cancellation-fee", headers=admin["headers"])).status_code == 403
    assert (await client.get(f"/rides/{trip['id']}/cancellation-fee")).status_code == 401
    assert (await client.get("/rides/99999/cancellation-fee", headers=trip["rider"]["headers"])).status_code == 404

    assert (await row(db, trip["id"]), await event_count(db, trip["id"])) == before


async def test_the_quote_is_refused_once_the_ride_can_no_longer_be_cancelled(client, make_user, insert_ride, trip):
    await go(client, trip, "arrive", "start")
    assert (await quote(client, trip)).status_code == 409  # IN_PROGRESS
    await go(client, trip, "complete")
    assert (await quote(client, trip)).status_code == 409  # COMPLETED

    for status in (RideStatus.CANCELLED, RideStatus.NO_DRIVER_FOUND):
        who = await make_user("rider")
        ride = await insert_ride(who, status)
        assert (await quote(client, {"rider": who, "id": ride.id})).status_code == 409, status


async def test_cancel_notifications_still_go_out_and_carry_no_money(client, open_socket, trip):
    rider_ws = await open_socket(trip["rider"])
    driver_ws = await open_socket(trip["driver"])

    assert (await cancel(client, trip)).status_code == 200

    expected = {"type": "ride_updated", "data": {"ride_id": trip["id"], "status": "CANCELLED"}}
    for ws in (rider_ws, driver_ws):
        raw = await asyncio.wait_for(ws.recv(), 2)
        assert json.loads(raw) == expected  # nothing else in the message: no fee, no fare


async def test_complete_notifications_carry_no_money_either(client, fake_clock, open_socket, trip):
    await go(client, trip, "arrive", "start")
    rider_ws = await open_socket(trip["rider"])
    driver_ws = await open_socket(trip["driver"])

    await go(client, trip, "complete")

    expected = {"type": "ride_updated", "data": {"ride_id": trip["id"], "status": "COMPLETED"}}
    for ws in (rider_ws, driver_ws):
        assert json.loads(await asyncio.wait_for(ws.recv(), 2)) == expected
