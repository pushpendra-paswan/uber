import pytest

from app.config import settings
from app.database import redis_client
from app.models import User, UserRole
from app.security import create_access_token

GEO_KEY = "drivers:geo"



# Written out by hand, not imported from the repository, so a wrong change to a key name fails the tests.
def presence_key(driver_id: int) -> str:
    return f"driver:{driver_id}:presence"


# Derived from the configured city, so they are inside it whichever city .env names. The two are different places.
INSIDE = {"lat": settings.city_center_lat, "lng": settings.city_center_lng}
OTHER_INSIDE = {"lat": (settings.city_center_lat + settings.city_south) / 2, "lng": (settings.city_center_lng + settings.city_east) / 2}
OUTSIDE = {"lat": settings.city_south - 1, "lng": settings.city_center_lng}

PRESENCE_ROUTES = [
    ("POST", "/drivers/me/online", INSIDE),
    ("POST", "/drivers/me/offline", None),
    ("POST", "/drivers/me/location", INSIDE),
    ("GET", "/drivers/me/presence", None),
]


async def redis_has_driver(driver_id: int) -> bool:
    in_geo = await redis_client.geopos(GEO_KEY, str(driver_id))
    return in_geo[0] is not None or await redis_client.exists(presence_key(driver_id)) == 1


@pytest.mark.parametrize("method, path, body", PRESENCE_ROUTES)
async def test_presence_routes_need_a_driver_token(client, rider, admin, method, path, body):
    assert (await client.request(method, path, json=body)).status_code == 401
    assert (await client.request(method, path, json=body, headers=rider["headers"])).status_code == 403
    assert (await client.request(method, path, json=body, headers=admin["headers"])).status_code == 403


@pytest.mark.parametrize("method, path, body", PRESENCE_ROUTES)
async def test_driver_without_a_profile_gets_404(client, db, method, path, body):
    user = User(role=UserRole.driver, name="No profile", email="noprofile@example.com", password_hash="unused")
    db.add(user)
    await db.commit()
    headers = {"Authorization": f"Bearer {create_access_token(user)}"}

    response = await client.request(method, path, json=body, headers=headers)

    assert response.status_code == 404


async def test_pending_and_rejected_drivers_are_forbidden(client, admin, make_user):
    pending = await make_user("driver", approved=False)
    rejected = await make_user("driver")
    await client.post(f"/admin/drivers/{rejected['driver'].id}/reject", headers=admin["headers"])

    for who in (pending, rejected):
        online = await client.post("/drivers/me/online", json=INSIDE, headers=who["headers"])
        ping = await client.post("/drivers/me/location", json=INSIDE, headers=who["headers"])
        assert online.status_code == 403
        assert ping.status_code == 403
        assert not await redis_has_driver(who["driver"].id)
    assert online.json()["detail"] == "Your driver account is not approved (status: rejected)"


async def test_go_online_writes_geo_member_and_presence_key(client, driver):
    response = await client.post("/drivers/me/online", json=INSIDE, headers=driver["headers"])

    assert response.status_code == 200
    body = response.json()
    assert body["online"] is True
    assert body["updated_at"] > 0
    # The GEO member is drivers.id, not users.id.
    driver_id = driver["driver"].id
    [(lng, lat)] = await redis_client.geopos(GEO_KEY, str(driver_id))
    assert lat == pytest.approx(INSIDE["lat"], abs=0.0001)
    assert lng == pytest.approx(INSIDE["lng"], abs=0.0001)
    assert await redis_client.get(presence_key(driver_id)) == str(body["updated_at"])
    assert 1 <= await redis_client.ttl(presence_key(driver_id)) <= 30


async def test_longitude_is_first_in_redis_and_latitude_first_in_the_api(client, driver):
    # Latitude and longitude of the configured city differ by a lot, so a swap cannot hide.
    assert abs(INSIDE["lat"] - INSIDE["lng"]) > 1
    await client.post("/drivers/me/online", json=INSIDE, headers=driver["headers"])

    response = await client.get("/drivers/me/presence", headers=driver["headers"])

    assert response.json()["lat"] == pytest.approx(INSIDE["lat"], abs=0.0001)
    assert response.json()["lng"] == pytest.approx(INSIDE["lng"], abs=0.0001)
    [first, second] = (await redis_client.geopos(GEO_KEY, str(driver["driver"].id)))[0]
    assert first == pytest.approx(INSIDE["lng"], abs=0.0001)
    assert second == pytest.approx(INSIDE["lat"], abs=0.0001)


@pytest.mark.parametrize(
    "body, expected",
    [(OUTSIDE, 422), ({"lat": 91, "lng": INSIDE["lng"]}, 422), ({"lat": INSIDE["lat"]}, 422), ({"lat": "x", "lng": 1}, 422)],
)
async def test_go_online_rejects_bad_points_and_writes_nothing(client, driver, body, expected):
    response = await client.post("/drivers/me/online", json=body, headers=driver["headers"])

    assert response.status_code == expected
    assert not await redis_has_driver(driver["driver"].id)
    assert await redis_client.dbsize() == 0


async def test_outside_the_city_message(client, driver):
    response = await client.post("/drivers/me/online", json=OUTSIDE, headers=driver["headers"])

    assert response.json()["detail"] == "Location is outside the service area"


async def test_a_ping_while_online_moves_the_driver_and_resets_the_ttl(client, driver):
    await client.post("/drivers/me/online", json=INSIDE, headers=driver["headers"])
    driver_id = driver["driver"].id
    await redis_client.expire(presence_key(driver_id), 5)

    response = await client.post("/drivers/me/location", json=OTHER_INSIDE, headers=driver["headers"])

    assert response.status_code == 200
    assert response.json()["online"] is True
    assert await redis_client.ttl(presence_key(driver_id)) > 25
    [(lng, lat)] = await redis_client.geopos(GEO_KEY, str(driver_id))
    assert lat == pytest.approx(OTHER_INSIDE["lat"], abs=0.0001)
    assert lng == pytest.approx(OTHER_INSIDE["lng"], abs=0.0001)
    # A ping outside the city is refused and leaves the position alone.
    assert (await client.post("/drivers/me/location", json=OUTSIDE, headers=driver["headers"])).status_code == 422
    [(_, lat_after)] = await redis_client.geopos(GEO_KEY, str(driver_id))
    assert lat_after == pytest.approx(OTHER_INSIDE["lat"], abs=0.0001)


async def test_a_ping_while_offline_is_409_and_writes_nothing(client, driver):
    response = await client.post("/drivers/me/location", json=INSIDE, headers=driver["headers"])

    assert response.status_code == 409
    assert response.json()["detail"] == "You are offline. Go online first."
    assert not await redis_has_driver(driver["driver"].id)


async def test_expired_presence_means_offline_but_the_geo_member_stays(client, driver):
    await client.post("/drivers/me/online", json=INSIDE, headers=driver["headers"])
    driver_id = driver["driver"].id
    await redis_client.delete(presence_key(driver_id))  # what the TTL does after 30 seconds

    ping = await client.post("/drivers/me/location", json=INSIDE, headers=driver["headers"])
    presence = await client.get("/drivers/me/presence", headers=driver["headers"])

    assert ping.status_code == 409
    assert presence.json() == {"online": False, "lat": None, "lng": None, "updated_at": None}
    # Known behavior: the GEO set is only an index and keeps stale members. Readers must check the presence key.
    assert (await redis_client.geopos(GEO_KEY, str(driver_id)))[0] is not None


async def test_go_offline_removes_both_keys_and_works_twice(client, driver):
    await client.post("/drivers/me/online", json=INSIDE, headers=driver["headers"])

    first = await client.post("/drivers/me/offline", headers=driver["headers"])
    second = await client.post("/drivers/me/offline", headers=driver["headers"])

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["online"] is False
    assert not await redis_has_driver(driver["driver"].id)
    assert (await client.get("/drivers/me/presence", headers=driver["headers"])).json()["online"] is False


async def test_going_offline_is_blocked_by_an_active_ride(client, rider, driver, assign_ride):
    # The ride comes from an accepted offer: the driver is the only one online and sits at the pickup.
    ride_id = (await assign_ride(rider, driver))["id"]

    blocked = await client.post("/drivers/me/offline", headers=driver["headers"])

    assert blocked.status_code == 409
    assert blocked.json()["detail"] == "Finish or cancel your current ride before going offline"
    assert (await client.get("/drivers/me/presence", headers=driver["headers"])).json()["online"] is True

    await client.post(f"/rides/{ride_id}/cancel", headers=rider["headers"])
    allowed = await client.post("/drivers/me/offline", headers=driver["headers"])

    assert allowed.status_code == 200
    assert not await redis_has_driver(driver["driver"].id)


async def test_rejecting_an_online_driver_takes_them_offline(client, driver, admin):
    await client.post("/drivers/me/online", json=INSIDE, headers=driver["headers"])
    driver_id = driver["driver"].id

    response = await client.post(f"/admin/drivers/{driver_id}/reject", headers=admin["headers"])

    assert response.status_code == 200
    assert not await redis_has_driver(driver_id)
    ping = await client.post("/drivers/me/location", json=INSIDE, headers=driver["headers"])
    assert ping.status_code == 403
    # Approving again does not bring them online by itself, but they can go online.
    await client.post(f"/admin/drivers/{driver_id}/approve", headers=admin["headers"])
    assert (await client.get("/drivers/me/presence", headers=driver["headers"])).json()["online"] is False
    assert (await client.post("/drivers/me/online", json=INSIDE, headers=driver["headers"])).status_code == 200


async def test_two_drivers_are_independent(client, driver, make_user):
    other = await make_user("driver")
    await client.post("/drivers/me/online", json=INSIDE, headers=driver["headers"])
    await client.post("/drivers/me/online", json=OTHER_INSIDE, headers=other["headers"])

    await client.post("/drivers/me/offline", headers=driver["headers"])

    assert not await redis_has_driver(driver["driver"].id)
    assert await redis_client.exists(presence_key(other["driver"].id)) == 1
    presence = (await client.get("/drivers/me/presence", headers=other["headers"])).json()
    assert presence["online"] is True
    assert presence["lat"] == pytest.approx(OTHER_INSIDE["lat"], abs=0.0001)
    assert await redis_client.zcard(GEO_KEY) == 1
