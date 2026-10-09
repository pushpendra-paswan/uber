import pytest
from fastapi import HTTPException
from sqlalchemy import delete, select

from app.config import settings
from app.models import PricingRule, Ride, RideStatus
from app.services import routing
from app.services.pricing import calculate_fare
from app.services.rides import MIN_TRIP_DISTANCE_M
from test_rides import RIDE_BODY

ESTIMATE_BODY = {key: value for key, value in RIDE_BODY.items() if not key.endswith("address")}


# Seed rule, worked out by hand: base 5000, Rs 12 per km = 1200, Rs 2 per minute = 200, minimum 8000 (paise).
@pytest.mark.parametrize(
    "distance_m, duration_s, distance_fare, time_fare, fare, minimum_applied",
    [
        (5000, 900, 6000, 3000, 14000, False),  # 5 km * 1200 = 6000; 15 min * 200 = 3000; 5000 + 6000 + 3000
        (1000, 180, 1200, 600, 8000, True),  # subtotal 5000 + 1200 + 600 = 6800, below the minimum
        (2000, 180, 2400, 600, 8000, False),  # subtotal is exactly 8000: the minimum did not need to apply
        (10000, 1500, 12000, 5000, 22000, False),  # 10 km * 1200; 25 min * 200; 5000 + 12000 + 5000
        (1234, 100, 1481, 333, 8000, True),  # 1480.8 rounds to 1481; 333.33 rounds to 333; subtotal 6814
    ],
)
async def test_fare_formula_with_the_seed_rule(db, distance_m, duration_s, distance_fare, time_fare, fare, minimum_applied):
    result = await calculate_fare(db, distance_m, duration_s)
    assert result == {
        "base_fare": 5000,
        "distance_fare": distance_fare,
        "time_fare": time_fare,
        "fare_estimate": fare,
        "minimum_fare_applied": minimum_applied,
        "normal_fare": fare,
        "surge_percent": 100,
        "surge_amount": 0,
    }


# A rule with per_km 1 and per_min 1 makes the rounding visible: 0.5 paise must become 1, 0.49 must become 0.
@pytest.mark.parametrize(
    "distance_m, duration_s, distance_fare, time_fare",
    [
        (500, 0, 1, 0),
        (499, 0, 0, 0),
        (1500, 0, 2, 0),
        (1499, 0, 1, 0),
        (0, 30, 0, 1),
        (0, 29, 0, 0),
        (0, 90, 0, 2),
        (0, 89, 0, 1),
    ],
)
async def test_fare_rounds_half_up(db, distance_m, duration_s, distance_fare, time_fare):
    db.add(PricingRule(vehicle_type="test", base_fare=0, per_km=1, per_min=1, min_fare=0))
    await db.commit()

    result = await calculate_fare(db, distance_m, duration_s, vehicle_type="test")

    assert result["distance_fare"] == distance_fare
    assert result["time_fare"] == time_fare
    assert result["fare_estimate"] == distance_fare + time_fare


async def test_estimate_returns_route_and_a_breakdown_that_adds_up(client, rider):
    response = await client.post("/rides/estimate", json=ESTIMATE_BODY, headers=rider["headers"])

    assert response.status_code == 200
    estimate = response.json()
    assert estimate["distance_m"] == 5000
    assert estimate["duration_s"] == 900
    assert estimate["fare_estimate"] == 14000
    assert estimate["minimum_fare_applied"] is False
    assert (estimate["normal_fare"], estimate["surge_percent"], estimate["surge_amount"]) == (14000, 100, 0)
    assert estimate["base_fare"] + estimate["distance_fare"] + estimate["time_fare"] == estimate["fare_estimate"]
    assert estimate["path"] == [
        [ESTIMATE_BODY["pickup_lat"], ESTIMATE_BODY["pickup_lng"]],
        [ESTIMATE_BODY["dropoff_lat"], ESTIMATE_BODY["dropoff_lng"]],
    ]


@pytest.mark.parametrize(
    "changes, message",
    [
        ({"pickup_lat": settings.city_north + 0.01}, "Pickup is outside the service area"),
        ({"pickup_lng": settings.city_west - 0.01}, "Pickup is outside the service area"),
        ({"dropoff_lat": settings.city_south - 0.01}, "Drop-off is outside the service area"),
        ({"dropoff_lng": settings.city_east + 0.01}, "Drop-off is outside the service area"),
    ],
    ids=["pickup-north", "pickup-west", "dropoff-south", "dropoff-east"],
)
async def test_estimate_outside_the_city_is_422(client, rider, changes, message):
    response = await client.post("/rides/estimate", json={**ESTIMATE_BODY, **changes}, headers=rider["headers"])

    assert response.status_code == 422
    assert response.json()["detail"] == message


async def test_estimate_with_the_same_point_twice_is_422(client, rider):
    same = {"dropoff_lat": ESTIMATE_BODY["pickup_lat"], "dropoff_lng": ESTIMATE_BODY["pickup_lng"]}
    response = await client.post("/rides/estimate", json={**ESTIMATE_BODY, **same}, headers=rider["headers"])
    assert response.status_code == 422


@pytest.mark.parametrize("distance_m, expected_status", [(MIN_TRIP_DISTANCE_M - 1, 422), (MIN_TRIP_DISTANCE_M, 200)])
async def test_estimate_for_a_very_short_route(client, rider, monkeypatch, distance_m, expected_status):
    async def short_route(*points):
        return {"distance_m": distance_m, "duration_s": 60, "path": []}

    monkeypatch.setattr(routing, "get_route", short_route)

    response = await client.post("/rides/estimate", json=ESTIMATE_BODY, headers=rider["headers"])

    assert response.status_code == expected_status
    if expected_status == 422:
        assert response.json()["detail"] == "Pickup and drop-off are too close for a ride"


async def test_estimate_when_there_is_no_route_is_422(client, rider, monkeypatch):
    async def no_route(*points):
        raise HTTPException(status_code=422, detail="No route found between these points. Choose points closer to a road.")

    monkeypatch.setattr(routing, "get_route", no_route)

    response = await client.post("/rides/estimate", json=ESTIMATE_BODY, headers=rider["headers"])

    assert response.status_code == 422
    assert response.json()["detail"].startswith("No route found")


async def test_estimate_without_a_pricing_rule_is_503(client, rider, db):
    await db.execute(delete(PricingRule))
    await db.commit()

    response = await client.post("/rides/estimate", json=ESTIMATE_BODY, headers=rider["headers"])

    assert response.status_code == 503
    assert response.json()["detail"] == "Pricing is not configured"


async def test_ride_stores_the_servers_estimate_and_ignores_the_clients(client, rider, db):
    estimate = (await client.post("/rides/estimate", json=ESTIMATE_BODY, headers=rider["headers"])).json()
    cheating = {"fare_estimate": 1, "distance_m": 1, "duration_s": 1, "final_fare": 1, "status": "COMPLETED"}

    response = await client.post("/rides", json={**RIDE_BODY, **cheating}, headers=rider["headers"])

    assert response.status_code == 201
    ride = response.json()
    assert ride["status"] == "NO_DRIVER_FOUND"  # nobody is online in this test
    assert ride["final_fare"] is None
    assert (ride["distance_m"], ride["duration_s"], ride["fare_estimate"]) == (
        estimate["distance_m"],
        estimate["duration_s"],
        estimate["fare_estimate"],
    )
    stored = (await db.execute(select(Ride).where(Ride.id == ride["id"]))).scalar_one()
    assert (stored.distance_m, stored.duration_s, stored.fare_estimate) == (5000, 900, 14000)
    fetched = await client.get(f"/rides/{ride['id']}", headers=rider["headers"])
    assert fetched.json()["fare_estimate"] == 14000


async def test_rider_with_an_active_ride_gets_409_before_any_routing(client, rider, insert_ride, monkeypatch):
    await insert_ride(rider, RideStatus.REQUESTED)

    async def broken_route(*points):
        raise HTTPException(status_code=502, detail="Routing is unavailable")

    monkeypatch.setattr(routing, "get_route", broken_route)

    response = await client.post("/rides", json=RIDE_BODY, headers=rider["headers"])

    assert response.status_code == 409


async def test_only_riders_can_estimate(client, driver, admin):
    assert (await client.post("/rides/estimate", json=ESTIMATE_BODY, headers=driver["headers"])).status_code == 403
    assert (await client.post("/rides/estimate", json=ESTIMATE_BODY, headers=admin["headers"])).status_code == 403
    assert (await client.post("/rides/estimate", json=ESTIMATE_BODY)).status_code == 401
