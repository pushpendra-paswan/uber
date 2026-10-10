from datetime import datetime, timedelta, timezone

import pytest
from redis.exceptions import RedisError
from sqlalchemy import text

from app.database import redis_client
from app.models import Ride, RideEvent, RideOffer, RideStatus
from app.repositories import admin as admin_repo
from app.repositories import drivers as drivers_repo
from app.services import admin as admin_service
from test_edge_cases import drop_presence
from test_fares import go, trip  # noqa: F401  (a fixture: trip)
from test_payments import wallet_trip  # noqa: F401  (a fixture)
from test_ratings import rate
from test_rides import RIDE_BODY

LAT = RIDE_BODY["pickup_lat"]
LNG = RIDE_BODY["pickup_lng"]
T0 = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)
SECRET_CODE = "7391"  # a trip code that cannot appear by chance as a number: it is searched for between quotes

DRIVER_KEYS = {
    "id", "license_number", "verification_status", "created_at", "user", "vehicle", "user_id", "online", "state", "active_ride_id",
    "rating_count", "rating_average", "completed_trips",
}
ROW_KEYS = {
    "id", "created_at", "status", "rider_id", "driver_id", "pickup_address", "dropoff_address", "fare_estimate", "final_fare",
    "surge_percent", "payment_method",
}
LIVE_RIDE_KEYS = {
    "id", "status", "pickup_lat", "pickup_lng", "pickup_address", "dropoff_lat", "dropoff_lng", "dropoff_address", "driver_id",
    "rider_id", "rider_name", "created_at", "fare_estimate",
}
LIVE_DRIVER_KEYS = {"id", "name", "plate_number", "lat", "lng", "state", "active_ride_id"}


async def listed(client, who: dict, **params) -> list[dict]:
    response = await client.get("/admin/drivers", params={"limit": 200, **params}, headers=who["headers"])
    assert response.status_code == 200, response.text
    return response.json()


async def one_driver(client, admin: dict, driver: dict) -> dict:
    return next(row for row in await listed(client, admin) if row["id"] == driver["driver"].id)


async def add_ride(db, rider: dict, status: RideStatus, created_at: datetime, driver: dict | None = None, **extra) -> Ride:
    ride = Ride(
        rider_id=rider["user"].id, driver_id=driver["driver"].id if driver else None, status=status, created_at=created_at,
        pickup_lat=LAT, pickup_lng=LNG, pickup_address="MG Road", dropoff_lat=LAT + 0.01, dropoff_lng=LNG + 0.01,
        dropoff_address="Koramangala", distance_m=5000, duration_s=900, fare_estimate=14000, **extra,
    )
    db.add(ride)
    await db.commit()
    return ride


# --- who may call ---


@pytest.mark.parametrize(
    "path",
    [
        "/admin/live", "/admin/drivers", "/admin/rides", "/admin/rides/1",
        "/admin/stats?since=2026-03-01T00:00:00Z&until=2026-03-02T00:00:00Z",
    ],
)
async def test_only_an_admin_may_use_the_admin_views(client, rider, driver, path):
    assert (await client.get(path)).status_code == 401
    for who in (rider, driver):
        assert (await client.get(path, headers=who["headers"])).status_code == 403


# --- 12. the drivers list ---


async def test_a_driver_row_has_every_documented_field_and_no_password_hash(client, admin, driver):
    response = await client.get("/admin/drivers", headers=admin["headers"])

    assert response.status_code == 200
    (row,) = response.json()
    assert set(row) == DRIVER_KEYS
    assert row["user_id"] == driver["user"].id
    assert (row["online"], row["state"], row["active_ride_id"]) == (False, "offline", None)
    assert (row["rating_count"], row["rating_average"], row["completed_trips"]) == (0, None, 0)
    assert "password" not in response.text


async def test_the_average_is_the_real_one_even_with_a_single_rating(client, admin, rider, driver, completed_ride):
    ride = await completed_ride(rider, driver)
    assert (await rate(client, rider, ride.id, score=4)).status_code == 201

    row = await one_driver(client, admin, driver)

    assert (row["rating_count"], row["rating_average"]) == (1, 4.0)


async def test_completed_trips_counts_completed_rides_only(client, db, admin, rider, driver, completed_ride, insert_ride):
    await completed_ride(rider, driver)
    await completed_ride(rider, driver)
    await add_ride(db, rider, RideStatus.CANCELLED, T0, driver)
    await add_ride(db, rider, RideStatus.NO_DRIVER_FOUND, T0)

    assert (await one_driver(client, admin, driver))["completed_trips"] == 2


async def test_the_state_of_a_driver_follows_the_rides_then_the_presence_then_the_offers(
    client, db, admin, make_user, put_online, accept_offer
):
    offline = await make_user("driver")
    free = await make_user("driver")
    offered = await make_user("driver")
    on_ride = await make_user("driver")
    rider_for_offer, rider_for_ride = await make_user("rider"), await make_user("rider")
    await put_online(free, LAT - 0.1, LNG)  # 11 km from every pickup (the radius is 3 km), so it is never offered a ride

    await put_online(on_ride, LAT, LNG)
    assert (await client.post("/rides", json=RIDE_BODY, headers=rider_for_ride["headers"])).status_code == 201
    await accept_offer(on_ride)
    await put_online(offered, LAT + 0.001, LNG)
    assert (await client.post("/rides", json=RIDE_BODY, headers=rider_for_offer["headers"])).status_code == 201

    states = {name: await one_driver(client, admin, who) for name, who in
              {"offline": offline, "free": free, "offered": offered, "on_ride": on_ride}.items()}

    assert {name: (row["state"], row["online"]) for name, row in states.items()} == {
        "offline": ("offline", False), "free": ("free", True), "offered": ("offered", True), "on_ride": ("on_ride", True),
    }
    assert states["on_ride"]["active_ride_id"] is not None and states["offered"]["active_ride_id"] is None


@pytest.mark.parametrize("steps", [[], ["arrive"], ["arrive", "start"]])
async def test_assigned_arrived_and_in_progress_drivers_are_on_a_ride_even_when_their_presence_expired(client, admin, trip, steps):
    await go(client, trip, *steps)
    row = await one_driver(client, admin, trip["driver"])
    assert (row["state"], row["online"], row["active_ride_id"]) == ("on_ride", True, trip["id"])

    await drop_presence(trip["driver"])
    row = await one_driver(client, admin, trip["driver"])

    assert (row["state"], row["online"], row["active_ride_id"]) == ("on_ride", False, trip["id"])


async def test_the_status_filter_and_an_invalid_status(client, admin, make_user):
    approved = await make_user("driver")
    pending = await make_user("driver", approved=False)

    assert [row["id"] for row in await listed(client, admin, status="pending")] == [pending["driver"].id]
    assert [row["id"] for row in await listed(client, admin, status="approved")] == [approved["driver"].id]
    assert await listed(client, admin, status="rejected") == []
    assert (await client.get("/admin/drivers", params={"status": "banned"}, headers=admin["headers"])).status_code == 422


# --- 13. search ---


async def named_drivers(db, make_user) -> dict:
    """Drivers whose names are the odd ones from the tests: a percent sign, an underscore, a backslash."""
    drivers = {name: await make_user("driver") for name in ("asha", "percent", "underscore", "backslash")}
    names = {"asha": "Asha Rao", "percent": "50% Off", "underscore": "snake_case", "backslash": "back\\slash"}
    for key, who in drivers.items():
        await db.execute(text("UPDATE users SET name = :name WHERE id = :id"), {"name": names[key], "id": who["user"].id})
    await db.commit()
    return drivers


async def ids_found(client, admin: dict, q: str) -> list[int]:
    return [row["id"] for row in await listed(client, admin, q=q)]


async def test_search_matches_name_email_plate_and_license_ignoring_case(client, db, admin, make_user):
    drivers = await named_drivers(db, make_user)
    asha = drivers["asha"]["driver"].id
    email = drivers["asha"]["user"].email
    unique = email.split("-")[1].split("@")[0]  # the 8 random characters shared by this driver's email, plate and license

    assert await ids_found(client, admin, "aSHa rao") == [asha]
    assert await ids_found(client, admin, email.upper()) == [asha]
    assert await ids_found(client, admin, f"plate{unique}") == [asha]  # the stored plate is upper case
    assert await ids_found(client, admin, f"lic-{unique}".upper()) == [asha]
    assert await ids_found(client, admin, "nobody by this name") == []


async def test_a_numeric_search_also_matches_the_driver_id(client, db, admin, make_user):
    drivers = await named_drivers(db, make_user)
    last = drivers["backslash"]["driver"].id

    assert last in await ids_found(client, admin, str(last))
    assert await ids_found(client, admin, "99999999") == []


async def test_percent_underscore_and_backslash_in_a_search_match_themselves(client, db, admin, make_user):
    drivers = await named_drivers(db, make_user)

    assert await ids_found(client, admin, "%") == [drivers["percent"]["driver"].id]  # not "everything"
    assert await ids_found(client, admin, "_") == [drivers["underscore"]["driver"].id]  # not "any one character"
    assert await ids_found(client, admin, "\\") == [drivers["backslash"]["driver"].id]
    assert await ids_found(client, admin, "50%") == [drivers["percent"]["driver"].id]
    assert await ids_found(client, admin, "snake_c") == [drivers["underscore"]["driver"].id]
    assert await ids_found(client, admin, "snakeXcase") == []
    assert await ids_found(client, admin, "\\%") == []


@pytest.mark.parametrize("q", ["x" * 101, "   ", ""])
async def test_a_search_that_is_too_long_or_blank_is_a_422(client, admin, driver, q):
    response = await client.get("/admin/drivers", params={"q": q}, headers=admin["headers"])

    assert response.status_code == 422


async def test_a_search_of_exactly_100_characters_is_accepted(client, admin, driver):
    assert (await client.get("/admin/drivers", params={"q": "x" * 100}, headers=admin["headers"])).status_code == 200


# --- 14. paging ---


async def test_paging_by_after_id_returns_every_driver_once_in_id_order(client, admin, make_user):
    created = [(await make_user("driver"))["driver"].id for _ in range(25)]

    seen, after_id = [], None
    for _ in range(10):
        params = {"limit": 10, **({"after_id": after_id} if after_id else {})}
        page = (await client.get("/admin/drivers", params=params, headers=admin["headers"])).json()
        if not page:
            break
        assert len(page) <= 10
        seen += [row["id"] for row in page]
        after_id = page[-1]["id"]

    assert seen == sorted(created)
    assert (await client.get("/admin/drivers", params={"after_id": max(created)}, headers=admin["headers"])).json() == []


@pytest.mark.parametrize("limit", [0, 201, -5])
async def test_a_drivers_limit_outside_1_to_200_is_a_422(client, admin, limit):
    assert (await client.get("/admin/drivers", params={"limit": limit}, headers=admin["headers"])).status_code == 422


async def test_the_default_drivers_limit_is_50(client, admin, make_user):
    for _ in range(52):
        await make_user("driver")

    assert len((await client.get("/admin/drivers", headers=admin["headers"])).json()) == 50


async def test_approve_and_reject_still_work(client, admin, make_user):
    pending = await make_user("driver", approved=False)

    approved = await client.post(f"/admin/drivers/{pending['driver'].id}/approve", headers=admin["headers"])
    rejected = await client.post(f"/admin/drivers/{pending['driver'].id}/reject", headers=admin["headers"])
    again = await client.post(f"/admin/drivers/{pending['driver'].id}/reject", headers=admin["headers"])

    assert (approved.status_code, approved.json()["verification_status"]) == (200, "approved")
    assert (rejected.status_code, rejected.json()["verification_status"]) == (200, "rejected")
    assert again.status_code == 409


# --- 15. the rides list ---


async def test_rides_come_newest_first_and_each_filter_works_alone_and_together(client, db, admin, make_user):
    rider_a, rider_b = await make_user("rider"), await make_user("rider")
    driver_a, driver_b = await make_user("driver"), await make_user("driver")
    rides = [
        await add_ride(db, rider_a, RideStatus.COMPLETED, T0, driver_a),
        await add_ride(db, rider_a, RideStatus.CANCELLED, T0 + timedelta(hours=1), driver_b),
        await add_ride(db, rider_b, RideStatus.COMPLETED, T0 + timedelta(hours=2), driver_a),
        await add_ride(db, rider_b, RideStatus.NO_DRIVER_FOUND, T0 + timedelta(hours=3)),
    ]
    a, b, c, d = [ride.id for ride in rides]

    async def ids(**params) -> list[int]:
        response = await client.get("/admin/rides", params=params, headers=admin["headers"])
        assert response.status_code == 200, response.text
        return [row["id"] for row in response.json()]

    assert await ids() == [d, c, b, a]
    assert await ids(status="COMPLETED") == [c, a]
    assert await ids(rider_id=rider_a["user"].id) == [b, a]
    assert await ids(driver_id=driver_a["driver"].id) == [c, a]
    assert await ids(status="COMPLETED", rider_id=rider_b["user"].id, driver_id=driver_a["driver"].id) == [c]
    assert await ids(status="CANCELLED", rider_id=rider_b["user"].id) == []
    # since is inclusive and until is exclusive, with rides exactly on both edges.
    assert await ids(since=(T0 + timedelta(hours=1)).isoformat(), until=(T0 + timedelta(hours=3)).isoformat()) == [c, b]
    assert await ids(since=(T0 + timedelta(hours=3)).isoformat()) == [d]
    assert await ids(until=T0.isoformat()) == []
    assert await ids(until=(T0 + timedelta(seconds=1)).isoformat()) == [a]


@pytest.mark.parametrize(
    "params",
    [
        {"since": "2026-03-01T12:00:00"},
        {"until": "2026-03-01T12:00:00"},
        {"since": "2026-03-02T00:00:00Z", "until": "2026-03-01T00:00:00Z"},
        {"since": "2026-03-01T00:00:00Z", "until": "2026-03-01T00:00:00Z"},
        {"status": "FLYING"},
        {"limit": 0},
        {"limit": 101},
    ],
)
async def test_a_bad_rides_filter_is_a_422(client, admin, params):
    assert (await client.get("/admin/rides", params=params, headers=admin["headers"])).status_code == 422


async def test_paging_over_60_rides_returns_each_once(client, db, admin, rider):
    created = [(await add_ride(db, rider, RideStatus.COMPLETED, T0 + timedelta(minutes=index))).id for index in range(60)]

    seen, before_id = [], None
    for _ in range(10):
        params = {"limit": 25, **({"before_id": before_id} if before_id else {})}
        page = (await client.get("/admin/rides", params=params, headers=admin["headers"])).json()
        if not page:
            break
        seen += [row["id"] for row in page]
        before_id = page[-1]["id"]

    assert seen == sorted(created, reverse=True)
    assert len((await client.get("/admin/rides", headers=admin["headers"])).json()) == 25  # the default limit


async def test_the_rides_list_has_only_the_documented_keys_and_never_the_trip_code(client, db, admin, rider, driver, insert_ride):
    ride = await insert_ride(rider, RideStatus.DRIVER_ASSIGNED, driver)
    await db.execute(text("UPDATE rides SET otp = :code WHERE id = :id"), {"code": SECRET_CODE, "id": ride.id})
    await db.commit()

    response = await client.get("/admin/rides", headers=admin["headers"])

    (row,) = response.json()
    assert set(row) == ROW_KEYS
    assert '"otp"' not in response.text and f'"{SECRET_CODE}"' not in response.text and SECRET_CODE not in row.values()


# --- 16. the ride detail ---


async def test_the_detail_of_a_completed_wallet_ride_shows_everything_and_never_the_trip_code(client, db, admin, wallet_trip):
    await go(client, wallet_trip, "arrive", "start", "complete")
    rider, driver, ride_id = wallet_trip["rider"], wallet_trip["driver"], wallet_trip["id"]
    assert (await rate(client, rider, ride_id, score=5, comment="Smooth <b>ride</b>")).status_code == 201
    assert (await rate(client, driver, ride_id, score=2, comment="Late to the pickup")).status_code == 201
    db.add(RideEvent(ride_id=ride_id, from_status=RideStatus.COMPLETED, to_status=RideStatus.COMPLETED, actor_user_id=admin["user"].id))
    await db.commit()
    await db.execute(text("UPDATE rides SET otp = :code WHERE id = :id"), {"code": SECRET_CODE, "id": ride_id})
    await db.commit()

    response = await client.get(f"/admin/rides/{ride_id}", headers=admin["headers"])

    assert response.status_code == 200, response.text
    body = response.json()
    assert '"otp"' not in response.text and f'"{SECRET_CODE}"' not in response.text
    assert body["rider"] == {"id": rider["user"].id, "name": rider["user"].name, "email": rider["user"].email}
    assert body["driver"]["id"] == driver["driver"].id and body["driver"]["user_id"] == driver["user"].id
    assert (body["driver"]["name"], body["driver"]["email"]) == (driver["user"].name, driver["user"].email)
    assert body["driver"]["plate_number"] == f"PLATE{driver['user'].email.split('-')[1].split('@')[0]}".upper()
    assert [(event["to_status"], event["actor"]) for event in body["events"]] == [
        ("REQUESTED", "rider"), ("DRIVER_ASSIGNED", "driver"), ("DRIVER_ARRIVED", "driver"), ("IN_PROGRESS", "driver"),
        ("COMPLETED", "driver"), ("COMPLETED", "other"),
    ]
    assert [offer["status"] for offer in body["offers"]] == ["ACCEPTED"]
    assert body["offers"][0]["driver_id"] == driver["driver"].id and body["offers"][0]["responded_at"] is not None
    final_fare = body["final_fare"]
    assert body["payment"]["amount"] == final_fare and body["payment"]["method"] == "wallet" and body["payment"]["status"] == "succeeded"
    earning = body["earning"]
    assert earning["gross_amount"] == final_fare and earning["commission_percent"] == 20
    assert earning["platform_fee"] == (final_fare * 20 + 50) // 100 and earning["platform_fee"] + earning["driver_earning"] == final_fare
    assert [(rating["from_role"], rating["score"], rating["comment"]) for rating in body["ratings"]] == [
        ("rider", 5, "Smooth <b>ride</b>"), ("driver", 2, "Late to the pickup"),
    ]
    assert body["ratings"][0]["from_user_id"] == rider["user"].id and body["ratings"][0]["to_user_id"] == driver["user"].id
    assert body["fare_breakdown"]["kind"] == "trip" and body["fare_breakdown"]["computed_fare"] >= final_fare


async def test_the_detail_of_an_unsettled_ride_has_no_payment_earning_or_ratings(client, admin, rider, insert_ride):
    ride = await insert_ride(rider, RideStatus.REQUESTED)

    body = (await client.get(f"/admin/rides/{ride.id}", headers=admin["headers"])).json()

    assert (body["payment"], body["earning"], body["ratings"], body["driver"], body["offers"]) == (None, None, [], None, [])
    assert body["status"] == "REQUESTED" and body["rider"]["id"] == rider["user"].id


async def test_an_unknown_ride_is_a_404(client, admin):
    assert (await client.get("/admin/rides/99999", headers=admin["headers"])).status_code == 404


async def test_the_detail_of_an_active_ride_hides_the_code(client, db, admin, trip):
    await db.execute(text("UPDATE rides SET otp = :code WHERE id = :id"), {"code": SECRET_CODE, "id": trip["id"]})
    await db.commit()

    response = await client.get(f"/admin/rides/{trip['id']}", headers=admin["headers"])

    assert response.status_code == 200
    assert '"otp"' not in response.text and f'"{SECRET_CODE}"' not in response.text


# --- 17. the live snapshot ---


async def live(client, admin: dict) -> dict:
    response = await client.get("/admin/live", headers=admin["headers"])
    assert response.status_code == 200, response.text
    return response.json()


async def test_only_drivers_with_a_presence_key_are_on_the_map_with_their_own_coordinates(client, admin, make_user, put_online):
    online = await make_user("driver")
    await make_user("driver")  # offline
    await put_online(online, 12.97, 77.59)
    await redis_client.geoadd(drivers_repo.GEO_KEY, (77.6, 13.0, "9999"))  # a stale GEO member: no presence key, no driver row
    stale = await make_user("driver")
    await redis_client.geoadd(drivers_repo.GEO_KEY, (77.61, 13.01, str(stale["driver"].id)))  # a real driver whose key expired

    body = await live(client, admin)

    (driver,) = body["drivers"]
    assert set(driver) == LIVE_DRIVER_KEYS
    assert driver["id"] == online["driver"].id
    assert driver["lat"] == pytest.approx(12.97, abs=1e-4) and driver["lng"] == pytest.approx(77.59, abs=1e-4)  # lat is not lng
    assert (driver["state"], driver["active_ride_id"], driver["name"]) == ("free", None, online["user"].name)
    assert body["counts"]["drivers"] == {"online": 1, "free": 1, "offered": 0, "on_ride": 0}
    assert (body["drivers_total"], body["truncated"]) == (1, False)


async def test_the_live_states_and_the_active_rides_with_exact_counts(client, db, admin, make_user, put_online, accept_offer, insert_ride):
    on_ride, offered, free = await make_user("driver"), await make_user("driver"), await make_user("driver")
    riders = [await make_user("rider") for _ in range(4)]
    await put_online(free, LAT - 0.1, LNG)  # 11 km from every pickup, so it is never offered a ride
    await put_online(on_ride, LAT, LNG)
    assert (await client.post("/rides", json=RIDE_BODY, headers=riders[0]["headers"])).status_code == 201
    await accept_offer(on_ride)
    await put_online(offered, LAT + 0.001, LNG)
    assert (await client.post("/rides", json=RIDE_BODY, headers=riders[1]["headers"])).status_code == 201
    # Finished rides are not on the map; an extra IN_PROGRESS ride (another driver) is.
    await insert_ride(riders[2], RideStatus.COMPLETED)
    await insert_ride(riders[3], RideStatus.CANCELLED)

    body = await live(client, admin)

    assert {row["id"]: row["state"] for row in body["drivers"]} == {
        on_ride["driver"].id: "on_ride", offered["driver"].id: "offered", free["driver"].id: "free",
    }
    assert body["counts"]["drivers"] == {"online": 3, "free": 1, "offered": 1, "on_ride": 1}
    assert {row["status"] for row in body["rides"]} == {"REQUESTED", "DRIVER_ASSIGNED"}
    assert all(set(row) == LIVE_RIDE_KEYS for row in body["rides"])
    assert body["counts"]["rides"] == {"REQUESTED": 1, "DRIVER_ASSIGNED": 1, "DRIVER_ARRIVED": 0, "IN_PROGRESS": 0}
    assert body["rides_total"] == 2
    assigned = next(row for row in body["rides"] if row["status"] == "DRIVER_ASSIGNED")
    assert (assigned["pickup_lat"], assigned["pickup_lng"], assigned["pickup_address"]) == (LAT, LNG, RIDE_BODY["pickup_address"])
    assert (assigned["dropoff_address"], assigned["rider_name"]) == (RIDE_BODY["dropoff_address"], riders[0]["user"].name)
    assert assigned["driver_id"] == on_ride["driver"].id and assigned["rider_id"] == riders[0]["user"].id
    assert [row["id"] for row in body["rides"]] == sorted((row["id"] for row in body["rides"]), reverse=True)  # newest first


async def test_each_active_status_is_on_the_map(client, admin, make_user, insert_ride):
    wanted = [RideStatus.REQUESTED, RideStatus.DRIVER_ASSIGNED, RideStatus.DRIVER_ARRIVED, RideStatus.IN_PROGRESS]
    for status in wanted:
        driver = None if status == RideStatus.REQUESTED else await make_user("driver")
        await insert_ride(await make_user("rider"), status, driver)

    body = await live(client, admin)

    assert sorted(row["status"] for row in body["rides"]) == sorted(status.value for status in wanted)
    assert body["counts"]["rides"] == {status.value: 1 for status in wanted}


async def test_the_live_lists_are_capped_with_exact_totals_and_a_flag(client, admin, make_user, put_online, insert_ride, monkeypatch):
    monkeypatch.setattr(admin_service, "LIVE_MAX_DRIVERS", 2)
    monkeypatch.setattr(admin_service, "LIVE_MAX_RIDES", 3)
    drivers = [await make_user("driver") for _ in range(5)]
    for who in reversed(drivers):  # the order they go online must not decide who is kept
        await put_online(who, LAT, LNG)
    rides = [await insert_ride(await make_user("rider"), RideStatus.REQUESTED) for _ in range(5)]

    body = await live(client, admin)

    assert body["truncated"] is True
    assert [row["id"] for row in body["drivers"]] == sorted(who["driver"].id for who in drivers)[:2]  # the lowest ids
    assert [row["id"] for row in body["rides"]] == [ride.id for ride in reversed(rides)][:3]  # the newest
    assert (body["drivers_total"], body["rides_total"]) == (5, 5)
    assert body["counts"]["drivers"]["online"] == 2  # counted from the returned rows
    assert body["counts"]["rides"]["REQUESTED"] == 5  # exact


async def test_the_live_snapshot_has_no_personal_data_of_drivers_and_no_trip_code(client, db, admin, trip):
    await db.execute(text("UPDATE rides SET otp = :code WHERE id = :id"), {"code": SECRET_CODE, "id": trip["id"]})
    await db.commit()

    response = await client.get("/admin/live", headers=admin["headers"])

    driver = trip["driver"]
    for private in (driver["user"].email, driver["driver"].license_number, "phone", "license", "password"):
        assert private not in response.text
    assert trip["rider"]["user"].email not in response.text
    assert '"otp"' not in response.text and f'"{SECRET_CODE}"' not in response.text


async def test_with_redis_down_the_live_map_is_a_503(client, admin, monkeypatch):
    async def broken():
        raise RedisError("down")

    monkeypatch.setattr(drivers_repo, "get_online_positions", broken)

    response = await client.get("/admin/live", headers=admin["headers"])

    assert response.status_code == 503 and response.json()["detail"] == "Cache is unavailable"


async def test_the_state_query_tells_a_pending_offer_from_a_ride(db, make_user):
    # get_driver_states is the one query behind both the map and the list.
    assert await admin_repo.get_driver_states(db, []) == {}
    driver, rider = await make_user("driver"), await make_user("rider")
    ride = await add_ride(db, rider, RideStatus.REQUESTED, T0)
    db.add(RideOffer(ride_id=ride.id, driver_id=driver["driver"].id, pickup_distance_m=10, expires_at=T0 + timedelta(seconds=15)))
    await db.commit()

    states = await admin_repo.get_driver_states(db, [driver["driver"].id])

    assert states == {driver["driver"].id: {"active_ride_id": None, "has_pending_offer": True}}
