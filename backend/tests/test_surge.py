import json
import math
import random
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from fastapi import HTTPException
from redis.exceptions import RedisError
from sqlalchemy import delete, func, select, text

from app.config import settings
from app.database import redis_client
from app.models import PricingRule, Ride, RideOffer, RideStatus
from app.repositories import pricing as pricing_repo
from app.repositories import rides as rides_repo
from app.services import pricing, routing
from app.utils.geo import GEOHASH_ALPHABET, geohash_encode
from test_edge_cases import logged  # noqa: F401  (a fixture: caplog attached to uvicorn's logger)
from test_fares import age_assignment, complete, go, hand_fare, set_times, walk
from test_rides import RIDE_BODY

LAT = RIDE_BODY["pickup_lat"]
LNG = RIDE_BODY["pickup_lng"]
ZONE = geohash_encode(LAT, LNG, 5)
METERS_PER_DEGREE = math.radians(1) * 6371000
ESTIMATE_BODY = {key: value for key, value in RIDE_BODY.items() if not key.endswith("address")}

# Written out by hand, not imported from the app, so a wrong change to the key fails the tests.
SNAPSHOT_KEY = "surge:snapshot"
PRESENCE_KEY = "driver:{}:presence"

# A second zone about 10 km south of the pickup, and a spot in the pickup's own zone more than the 3 km matching radius
# away, where a driver counts as supply without ever getting an offer.
OTHER_LAT = LAT - 0.09
OTHER_ZONE = geohash_encode(OTHER_LAT, LNG, 5)
FAR_IN_ZONE = (LAT + 0.03, LNG)
assert OTHER_ZONE != ZONE and geohash_encode(*FAR_IN_ZONE, 5) == ZONE
assert settings.city_south < OTHER_LAT, "the city is too small for the second zone of these tests"


@pytest.fixture(autouse=True)
def surge_on(no_surge, monkeypatch):
    """The conftest fixture switched surge off for the whole suite; these tests want the real rule, and no cache unless they ask."""
    monkeypatch.setattr(pricing, "MIN_DEMAND_FOR_SURGE", 3)
    monkeypatch.setattr(pricing, "SURGE_CACHE_TTL_S", 0)


@pytest_asyncio.fixture
async def snapshot(client, admin):
    """Returns a function that recomputes the snapshot through the real endpoint and gives {zone: its numbers}."""

    async def take() -> dict:
        response = await client.get("/admin/surge?refresh=true", headers=admin["headers"])
        assert response.status_code == 200, response.text
        return {zone["zone"]: zone for zone in response.json()["zones"]}

    return take


@pytest_asyncio.fixture
async def add_demand(db, make_user):
    """Returns a function that gives `riders` (or that many new riders) one ride each in `zone`, in the given status,
    created `age_s` seconds ago. Straight into the database. Returns the riders."""

    async def add(riders: int | list, zone: str | None = ZONE, status=RideStatus.NO_DRIVER_FOUND, age_s: float = 0) -> list:
        if isinstance(riders, int):
            riders = [await make_user("rider") for _ in range(riders)]
        for who in riders:
            db.add(
                Ride(
                    rider_id=who["user"].id, status=status, pickup_zone=zone, pickup_lat=LAT, pickup_lng=LNG, pickup_address="MG Road",
                    dropoff_lat=LAT - 0.01, dropoff_lng=LNG, dropoff_address="Koramangala", distance_m=5000, duration_s=900,
                    fare_estimate=14000, created_at=datetime.now(timezone.utc) - timedelta(seconds=age_s),
                )
            )
        await db.commit()
        return riders

    return add


@pytest_asyncio.fixture
async def add_supply(make_user, put_online):
    """Returns a function that puts `count` new approved drivers online at a point (the pickup by default)."""

    async def add(count: int, lat: float = LAT, lng: float = LNG) -> list:
        drivers = [await make_user("driver") for _ in range(count)]
        for who in drivers:
            await put_online(who, lat, lng)
        return drivers

    return add


async def estimate(client, rider, **changes) -> dict:
    response = await client.post("/rides/estimate", json={**ESTIMATE_BODY, **changes}, headers=rider["headers"])
    assert response.status_code == 200, response.text
    return response.json()


# --- A. geohash_encode ---


def test_geohash_known_vectors():
    assert geohash_encode(42.6, -5.6, 5) == "ezs42"
    assert geohash_encode(57.64911, 10.40744, 11) == "u4pruydqqvj"


def test_geohash_prefixes_alphabet_and_edges():
    rng = random.Random(7)
    for _ in range(200):
        lat, lng = rng.uniform(-90, 90), rng.uniform(-180, 180)
        long_hash = geohash_encode(lat, lng, 12)
        assert len(long_hash) == 12 and set(long_hash) <= set(GEOHASH_ALPHABET)
        for precision in range(1, 12):
            short_hash = geohash_encode(lat, lng, precision)
            assert len(short_hash) == precision
            assert long_hash.startswith(short_hash)
        assert geohash_encode(lat, lng, 5) == geohash_encode(lat, lng, 5)
    for lat, lng in ((90, 180), (-90, -180), (0, 0), (90, -180), (-90, 180)):
        edge = geohash_encode(lat, lng, 5)
        assert len(edge) == 5 and set(edge) <= set(GEOHASH_ALPHABET)


def test_a_zone_border_splits_points_20_m_apart_and_keeps_points_on_one_side_together():
    # Walk south from the city center in steps of 1 m until the zone changes.
    meter = 1 / METERS_PER_DEGREE
    steps = 0
    while geohash_encode(LAT - steps * meter, LNG, 5) == ZONE:
        steps += 1
        assert steps < 20000, "no zone border within 20 km"
    border = LAT - (steps - 0.5) * meter  # between the last point inside and the first outside

    north_20, north_40 = border + 20 * meter, border + 40 * meter
    south_20, south_40 = border - 20 * meter, border - 40 * meter
    assert geohash_encode(north_20, LNG, 5) != geohash_encode(south_20, LNG, 5)  # 40 m apart across the line
    assert geohash_encode(north_20, LNG, 5) == geohash_encode(north_40, LNG, 5)  # 20 m apart, same side
    assert geohash_encode(south_20, LNG, 5) == geohash_encode(south_40, LNG, 5)


# --- B. snapshot, demand and supply ---


@pytest.mark.parametrize(
    "demand, supply, pressure, multiplier",
    [
        (3, 3, 100, 100),
        (4, 3, 133, 120),
        (3, 2, 150, 120),
        (5, 3, 166, 150),
        (6, 3, 200, 150),
        (7, 3, 233, 180),
        (9, 3, 300, 180),
        (10, 3, 333, 200),
        (3, 0, 300, 180),
        (8, 1, 800, 200),
        (2, 0, 200, 100),  # below the minimum demand there is never surge
        (2, 5, 40, 100),
    ],
)
async def test_the_step_table(snapshot, add_demand, add_supply, demand, supply, pressure, multiplier):
    await add_demand(demand)
    await add_supply(supply)

    zone = (await snapshot())[ZONE]

    assert (zone["demand"], zone["supply"], zone["pressure_percent"], zone["surge_percent"]) == (demand, supply, pressure, multiplier)


async def test_demand_counts_distinct_unmet_riders_in_the_window_and_the_zone(snapshot, add_demand, make_user):
    spammer = await make_user("rider")
    await add_demand([spammer], status=RideStatus.NO_DRIVER_FOUND)
    await add_demand([spammer], status=RideStatus.NO_DRIVER_FOUND)
    await add_demand([spammer], status=RideStatus.NO_DRIVER_FOUND)  # the same rider three times: one
    await add_demand(1, status=RideStatus.REQUESTED)  # waiting for a driver: counts
    for status in (
        RideStatus.CANCELLED, RideStatus.COMPLETED, RideStatus.DRIVER_ASSIGNED, RideStatus.DRIVER_ARRIVED, RideStatus.IN_PROGRESS,
    ):
        await add_demand(1, status=status)  # served, cancelled or being served: not unmet
    await add_demand(1, zone=OTHER_ZONE)  # another zone
    await add_demand(1, zone=None)  # a ride from before M5.2
    await add_demand(1, age_s=181)  # too old
    await add_demand(1, age_s=179)  # still in the window

    zones = await snapshot()

    assert zones[ZONE]["demand"] == 3  # the spammer, the REQUESTED rider, the rider of 179 seconds ago
    assert zones[OTHER_ZONE]["demand"] == 1
    assert set(zones) == {ZONE, OTHER_ZONE}  # NULL zones never appear


async def test_supply_counts_only_available_drivers_in_the_zone(
    db, snapshot, add_supply, add_demand, make_user, insert_ride, put_online
):
    _free, busy, offered, vanished, rejected = await add_supply(5)
    elsewhere = await make_user("driver")
    await put_online(elsewhere, OTHER_LAT, LNG)

    ride = await insert_ride(await make_user("rider"), RideStatus.DRIVER_ASSIGNED, busy)  # an active ride
    pending_ride = (await add_demand(1, status=RideStatus.REQUESTED))[0]
    pending_ride_id = await db.scalar(select(Ride.id).where(Ride.rider_id == pending_ride["user"].id))
    offer = RideOffer(
        ride_id=pending_ride_id, driver_id=offered["driver"].id, pickup_distance_m=100,
        expires_at=datetime.now(timezone.utc) + timedelta(seconds=15),
    )
    db.add(offer)
    await db.commit()
    await redis_client.delete(PRESENCE_KEY.format(vanished["driver"].id))  # the presence key expired
    await db.execute(text("UPDATE drivers SET verification_status = 'rejected' WHERE id = :id"), {"id": rejected["driver"].id})
    await db.commit()

    zones = await snapshot()

    assert zones[ZONE]["supply"] == 1  # only the first driver
    assert zones[OTHER_ZONE]["supply"] == 1  # the driver in the other zone

    # The ride ends and the offer closes: both drivers are free again.
    await db.execute(text("UPDATE rides SET status = 'COMPLETED' WHERE id = :id"), {"id": ride.id})
    await db.execute(text("UPDATE ride_offers SET status = 'EXPIRED' WHERE id = :id"), {"id": offer.id})
    await db.commit()

    assert (await snapshot())[ZONE]["supply"] == 3


@pytest.mark.parametrize("cap, quoted", [(1.5, 150), (1.0, 100), (2.0, 200)])
async def test_the_cap_comes_from_the_pricing_rule_and_the_snapshot_stays_uncapped(
    client, db, rider, snapshot, add_demand, add_supply, cap, quoted
):
    await add_demand(8)
    await add_supply(1)  # pressure 800: the table says 200
    await db.execute(text("UPDATE pricing_rules SET surge_cap = :cap"), {"cap": cap})
    await db.commit()

    zones = await snapshot()

    assert zones[ZONE]["surge_percent"] == 200  # the snapshot never holds the capped value
    assert (await estimate(client, rider))["surge_percent"] == quoted


async def test_the_snapshot_is_cached_and_shared(client, db, rider, admin, add_demand, monkeypatch):
    monkeypatch.setattr(pricing, "SURGE_CACHE_TTL_S", 15)
    await add_demand(4)
    calls = 0
    real = rides_repo.count_unmet_demand_by_zone

    async def counted(*args, **kwargs):
        nonlocal calls
        calls += 1
        return await real(*args, **kwargs)

    monkeypatch.setattr(rides_repo, "count_unmet_demand_by_zone", counted)

    first = await estimate(client, rider)
    second = await estimate(client, rider)

    assert calls == 1  # the second quote read the cache
    assert first["surge_percent"] == second["surge_percent"] == 200
    assert 1 <= await redis_client.ttl(SNAPSHOT_KEY) <= 15
    stored = json.loads(await redis_client.get(SNAPSHOT_KEY))
    assert stored["zones"][ZONE] == {"demand": 4, "supply": 0, "pressure_percent": 400, "surge_percent": 200}
    assert isinstance(stored["computed_at"], int)

    await redis_client.delete(SNAPSHOT_KEY)
    await estimate(client, rider)
    assert calls == 2  # no key: recomputed

    # force recomputes and rewrites even when the key is there.
    await redis_client.set(SNAPSHOT_KEY, json.dumps({"computed_at": 1, "zones": {}}), ex=15)
    forced = await pricing.get_surge_snapshot(db, force=True)
    assert calls == 3
    assert forced["zones"][ZONE]["demand"] == 4
    assert json.loads(await redis_client.get(SNAPSHOT_KEY)) == forced


async def test_a_ttl_of_zero_turns_the_cache_off(client, rider, add_demand, monkeypatch):
    await add_demand(4)
    calls = 0
    real = rides_repo.count_unmet_demand_by_zone

    async def counted(*args, **kwargs):
        nonlocal calls
        calls += 1
        return await real(*args, **kwargs)

    monkeypatch.setattr(rides_repo, "count_unmet_demand_by_zone", counted)

    await estimate(client, rider)
    await estimate(client, rider)

    assert calls == 2
    assert await redis_client.exists(SNAPSHOT_KEY) == 0  # never written


async def test_a_redis_failure_means_no_surge_and_one_warning(client, rider, add_demand, logged, monkeypatch):
    await add_demand(9)

    async def broken(*args, **kwargs):
        raise RedisError("connection lost")

    monkeypatch.setattr(pricing, "SURGE_CACHE_TTL_S", 15)
    monkeypatch.setattr(pricing_repo, "get_snapshot", broken)

    body = await estimate(client, rider)

    assert body["surge_percent"] == 100
    assert (body["fare_estimate"], body["normal_fare"], body["surge_amount"]) == (14000, 14000, 0)
    assert [record.levelname for record in logged.records if "surge snapshot" in record.getMessage()] == ["WARNING"]


# --- C. fare arithmetic and lock-in ---


@pytest.mark.parametrize("surge, fare, surge_amount", [(100, 14000, 0), (120, 16800, 2800), (150, 21000, 7000), (200, 28000, 14000)])
async def test_surge_multiplies_the_normal_fare(db, surge, fare, surge_amount):
    # 5 km, 15 min: base 5000 + 5 x 1200 + 15 x 200 = 14000 normal.
    result = await pricing.calculate_fare(db, 5000, 900, surge)

    assert (result["normal_fare"], result["surge_percent"], result["fare_estimate"], result["surge_amount"]) == (14000, surge, fare, surge_amount)
    assert (result["base_fare"], result["distance_fare"], result["time_fare"]) == (5000, 6000, 3000)  # the normal parts
    assert result["normal_fare"] + result["surge_amount"] == result["fare_estimate"]


async def test_the_minimum_fare_is_multiplied_too(db):
    result = await pricing.calculate_fare(db, 1000, 180, 200)  # 6800 subtotal, minimum 8000

    assert result["minimum_fare_applied"] is True
    assert (result["normal_fare"], result["fare_estimate"], result["surge_amount"]) == (8000, 16000, 8000)


@pytest.mark.parametrize("normal, surge, fare", [(1, 150, 2), (3, 110, 3), (1, 149, 1), (5, 110, 6)])
async def test_surge_rounds_half_up(db, monkeypatch, normal, surge, fare):
    # An in-memory rule: the database refuses a minimum fare below 100 paise (M6.2), and this test needs a minimum of 0.
    rule = PricingRule(vehicle_type="test", base_fare=normal, per_km=0, per_min=0, min_fare=0)

    async def get_rule(db, vehicle_type):
        return rule

    monkeypatch.setattr(pricing_repo, "get_rule", get_rule)

    result = await pricing.calculate_fare(db, 1000, 60, surge, vehicle_type="test")

    assert result["normal_fare"] == normal
    assert result["fare_estimate"] == fare  # 1.5 -> 2, 3.3 -> 3, 1.49 -> 1, 5.5 -> 6


async def test_the_estimate_shows_the_surge_of_its_zone_only(client, rider, snapshot, add_demand):
    await add_demand(4)  # no drivers: pressure 400, multiplier 200

    surging = await estimate(client, rider)
    calm = await estimate(client, rider, pickup_lat=OTHER_LAT)

    assert (surging["surge_percent"], surging["normal_fare"], surging["fare_estimate"], surging["surge_amount"]) == (200, 14000, 28000, 14000)
    assert (calm["surge_percent"], calm["fare_estimate"], calm["surge_amount"]) == (100, 14000, 0)
    assert "zone" not in surging


async def test_a_ride_stores_its_zone_and_multiplier_and_its_own_request_is_not_in_its_price(client, db, rider, snapshot, add_demand):
    await add_demand(2)  # one rider short of the minimum
    quote = await estimate(client, rider)
    assert quote["surge_percent"] == 100

    response = await client.post("/rides", json=RIDE_BODY, headers=rider["headers"])

    assert response.status_code == 201
    ride = response.json()
    assert (ride["surge_percent"], ride["fare_estimate"]) == (100, quote["fare_estimate"])
    stored = (await db.execute(select(Ride.pickup_zone, Ride.surge_percent).where(Ride.id == ride["id"]))).one()
    assert tuple(stored) == (geohash_encode(RIDE_BODY["pickup_lat"], RIDE_BODY["pickup_lng"], 5), 100)
    assert "pickup_zone" not in ride
    # The request made the zone's demand 3 (nobody was online), so the next rider pays surge, this one did not.
    assert (await snapshot())[ZONE]["surge_percent"] == 180


async def test_a_ride_created_in_a_surging_zone_keeps_that_multiplier(client, db, rider, add_demand, add_supply):
    await add_demand(4)
    await add_supply(1, *FAR_IN_ZONE)  # in the zone, outside the matching radius: pressure 400, multiplier 200

    quote = await estimate(client, rider)
    response = await client.post("/rides", json=RIDE_BODY, headers=rider["headers"])

    assert response.status_code == 201
    assert (quote["surge_percent"], quote["fare_estimate"]) == (200, 28000)
    ride = response.json()
    assert (ride["surge_percent"], ride["fare_estimate"]) == (200, 28000)


@pytest_asyncio.fixture
async def surged_trip(client, rider, driver, add_demand, add_supply, assign_ride):
    """A DRIVER_ASSIGNED ride created at 150: 5 unmet riders, and 3 free drivers (the ride's own, which is at the pickup,
    and two more in the zone but outside the matching radius)."""
    demand_riders = await add_demand(5)
    await add_supply(2, *FAR_IN_ZONE)
    ride = await assign_ride(rider, driver)
    assert (ride["surge_percent"], ride["fare_estimate"]) == (150, 21000)
    return {"rider": rider, "driver": driver, "id": ride["id"], "demand_riders": demand_riders}


async def test_the_trip_is_settled_with_the_multiplier_of_the_request_not_the_current_one(
    client, db, fake_clock, snapshot, surged_trip
):
    await go(client, surged_trip, "arrive", "start")
    await walk(client, fake_clock, surged_trip, [25 * i for i in range(40)])
    await set_times(db, surged_trip["id"], 600)
    # The demand is over: the zone is at 100 now, the ride stays at 150.
    await db.execute(delete(Ride).where(Ride.rider_id.in_([who["user"].id for who in surged_trip["demand_riders"]])))
    await db.commit()
    assert (await snapshot()).get(ZONE, {"surge_percent": 100})["surge_percent"] == 100

    ride = (await complete(client, surged_trip)).json()

    breakdown = ride["fare_breakdown"]
    normal = hand_fare(ride["actual_distance_m"], ride["actual_duration_s"])
    assert breakdown["surge_percent"] == 150
    assert breakdown["normal_fare"] == normal
    assert breakdown["computed_fare"] == (normal * 150 + 50) // 100 == ride["final_fare"]
    assert breakdown["surge_amount"] == breakdown["computed_fare"] - normal
    assert breakdown["capped"] is False


async def test_a_calm_ride_is_not_surged_when_the_zone_surges_before_it_ends(
    client, db, rider, driver, fake_clock, snapshot, add_demand, assign_ride
):
    ride = await assign_ride(rider, driver)
    assert ride["surge_percent"] == 100
    trip = {"rider": rider, "driver": driver, "id": ride["id"]}
    await go(client, trip, "arrive", "start")
    await walk(client, fake_clock, trip, [25 * i for i in range(40)])
    await set_times(db, trip["id"], 600)
    await add_demand(8)  # the driver is busy, so supply is 0: the zone is at 200 now
    assert (await snapshot())[ZONE]["surge_percent"] == 200

    done = (await complete(client, trip)).json()

    assert done["fare_breakdown"]["surge_percent"] == 100
    assert done["fare_breakdown"]["surge_amount"] == 0
    assert done["final_fare"] == hand_fare(done["actual_distance_m"], done["actual_duration_s"])


async def test_the_cap_still_applies_to_the_surged_estimate(client, db, fake_clock, surged_trip):
    await go(client, surged_trip, "arrive", "start")
    await walk(client, fake_clock, surged_trip, [25 * i for i in range(20)])
    await set_times(db, surged_trip["id"], 3 * 3600)  # a 3 hour trip

    ride = (await complete(client, surged_trip)).json()

    breakdown = ride["fare_breakdown"]
    assert breakdown["computed_fare"] > 31500
    assert breakdown["fare_cap"] == 31500  # 150 percent of the surged estimate of 21000
    assert breakdown["capped"] is True
    assert ride["final_fare"] == 31500


async def test_a_late_cancellation_is_never_surged_by_the_zone(client, db, rider, driver, snapshot, add_demand, assign_ride):
    ride = await assign_ride(rider, driver)
    await add_demand(8)
    assert (await snapshot())[ZONE]["surge_percent"] == 200  # the driver is busy, so no supply
    await age_assignment(db, ride["id"], 600)

    response = await client.post(f"/rides/{ride['id']}/cancel", headers=rider["headers"])

    assert response.status_code == 200
    assert response.json()["final_fare"] == 3000
    assert response.json()["fare_breakdown"] == {"kind": "cancellation", "fee": 3000, "reason": "late_cancellation", "cancelled_by": "rider"}


async def test_a_late_cancellation_is_never_surged_by_the_ride(client, db, surged_trip):
    await age_assignment(db, surged_trip["id"], 600)
    quote = await client.get(f"/rides/{surged_trip['id']}/cancellation-fee", headers=surged_trip["rider"]["headers"])
    assert quote.json() == {"fee": 3000, "reason": "late_cancellation"}  # the ride was requested at 1.5x

    response = await client.post(f"/rides/{surged_trip['id']}/cancel", headers=surged_trip["rider"]["headers"])

    assert response.status_code == 200
    assert response.json()["final_fare"] == 3000
    assert response.json()["fare_breakdown"]["fee"] == 3000


async def test_cancelling_after_the_driver_arrived_is_not_surged_either(client, surged_trip):
    await go(client, surged_trip, "arrive")
    headers = surged_trip["rider"]["headers"]
    quote = await client.get(f"/rides/{surged_trip['id']}/cancellation-fee", headers=headers)
    assert quote.json() == {"fee": 3000, "reason": "driver_arrived"}

    response = await client.post(f"/rides/{surged_trip['id']}/cancel", headers=headers)

    assert response.json()["final_fare"] == 3000


# --- D. the accepted multiplier ---


@pytest_asyncio.fixture
async def surging_at_150(add_demand, add_supply):
    """5 unmet riders and 3 free drivers in the zone, none within the matching radius of the pickup: pressure 166, 150."""
    await add_demand(5)
    await add_supply(3, *FAR_IN_ZONE)


async def ride_count(db) -> int:
    return await db.scalar(select(func.count()).select_from(Ride))


@pytest.mark.parametrize("accepted", [100, 149])
async def test_a_lower_accepted_multiplier_is_a_409_and_creates_nothing(client, db, rider, surging_at_150, accepted):
    before = await ride_count(db)

    response = await client.post("/rides", json={**RIDE_BODY, "accepted_surge_percent": accepted}, headers=rider["headers"])

    assert response.status_code == 409
    assert response.json()["detail"] == "Prices have increased in your area (now 1.5x). Please review the new fare and request again."
    assert await ride_count(db) == before
    assert await db.scalar(select(func.count()).select_from(RideOffer)) == 0


@pytest.mark.parametrize("accepted, charged", [(150, 150), (180, 150), (200, 150), (None, 150)])
async def test_an_equal_or_higher_or_missing_accepted_multiplier_is_charged_the_current_one(
    client, rider, surging_at_150, accepted, charged
):
    body = dict(RIDE_BODY) if accepted is None else {**RIDE_BODY, "accepted_surge_percent": accepted}

    response = await client.post("/rides", json=body, headers=rider["headers"])

    assert response.status_code == 201
    assert response.json()["surge_percent"] == charged
    assert response.json()["fare_estimate"] == 21000


@pytest.mark.parametrize("bad", [99, 201, "abc", 1.5, -1, 0])
async def test_an_invalid_accepted_multiplier_is_a_422_and_changes_nothing(client, db, rider, bad):
    response = await client.post("/rides", json={**RIDE_BODY, "accepted_surge_percent": bad}, headers=rider["headers"])

    assert response.status_code == 422
    assert await ride_count(db) == 0


async def test_an_active_ride_is_refused_before_the_multiplier_or_any_routing(client, rider, insert_ride, surging_at_150, monkeypatch):
    await insert_ride(rider, RideStatus.REQUESTED)

    async def broken_route(*points):
        raise HTTPException(status_code=502, detail="Routing is unavailable")

    monkeypatch.setattr(routing, "get_route", broken_route)

    response = await client.post("/rides", json={**RIDE_BODY, "accepted_surge_percent": 100}, headers=rider["headers"])

    assert response.status_code == 409
    assert response.json()["detail"] == "You already have an active ride"


# --- E. the admin endpoint ---


async def test_the_admin_snapshot_has_the_documented_shape_in_order(client, admin, monkeypatch):
    monkeypatch.setattr(pricing, "SURGE_CACHE_TTL_S", 15)
    now = int(datetime.now(timezone.utc).timestamp())

    def zone(demand, supply, pressure, surge):
        return {"demand": demand, "supply": supply, "pressure_percent": pressure, "surge_percent": surge}

    await pricing_repo.save_snapshot(
        {
            "computed_at": now - 10,
            "zones": {
                "ddddd": zone(1, 1, 100, 100), "eeeee": zone(1, 1, 100, 100), "aaaaa": zone(3, 2, 150, 120),
                "bbbbb": zone(5, 4, 125, 120), "ccccc": zone(9, 3, 300, 180),
            },
        },
        15,
    )

    body = (await client.get("/admin/surge", headers=admin["headers"])).json()

    assert set(body) == {"computed_at", "age_seconds", "zones"}
    assert body["computed_at"] == now - 10 and 10 <= body["age_seconds"] <= 12
    assert [item["zone"] for item in body["zones"]] == ["ccccc", "bbbbb", "aaaaa", "ddddd", "eeeee"]
    assert set(body["zones"][0]) == {"zone", "demand", "supply", "pressure_percent", "surge_percent"}
    assert body["zones"][0] == {"zone": "ccccc", "demand": 9, "supply": 3, "pressure_percent": 300, "surge_percent": 180}

    # The age grows with the snapshot's own clock, and refresh=true makes a new snapshot of age 0.
    await pricing_repo.save_snapshot({"computed_at": now - 40, "zones": {}}, 15)
    assert (await client.get("/admin/surge", headers=admin["headers"])).json()["age_seconds"] >= 40
    refreshed = (await client.get("/admin/surge?refresh=true", headers=admin["headers"])).json()
    assert refreshed["age_seconds"] <= 1 and refreshed["zones"] == []
    assert json.loads(await redis_client.get(SNAPSHOT_KEY))["computed_at"] == refreshed["computed_at"]


async def test_only_admins_can_read_the_snapshot(client, rider, driver):
    assert (await client.get("/admin/surge", headers=rider["headers"])).status_code == 403
    assert (await client.get("/admin/surge", headers=driver["headers"])).status_code == 403
    assert (await client.get("/admin/surge")).status_code == 401
