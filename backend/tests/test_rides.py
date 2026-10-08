import asyncio
import itertools

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select

from app.config import settings
from app.models import Ride, RideEvent, RideStatus
from app.services.rides import change_ride_status

# Both points are derived from the configured city, so they are inside it whichever city .env names.
RIDE_BODY = {
    "pickup_lat": settings.city_center_lat,
    "pickup_lng": settings.city_center_lng,
    "pickup_address": "MG Road",
    "dropoff_lat": (settings.city_center_lat + settings.city_south) / 2,
    "dropoff_lng": (settings.city_center_lng + settings.city_east) / 2,
    "dropoff_address": "Koramangala",
}

# Written out by hand, not imported from the app, so a wrong change to ALLOWED_TRANSITIONS fails the tests.
LEGAL_TRANSITIONS = {
    (RideStatus.REQUESTED, RideStatus.DRIVER_ASSIGNED),
    (RideStatus.REQUESTED, RideStatus.CANCELLED),
    (RideStatus.REQUESTED, RideStatus.NO_DRIVER_FOUND),
    (RideStatus.DRIVER_ASSIGNED, RideStatus.DRIVER_ARRIVED),
    (RideStatus.DRIVER_ASSIGNED, RideStatus.CANCELLED),
    (RideStatus.DRIVER_ARRIVED, RideStatus.IN_PROGRESS),
    (RideStatus.DRIVER_ARRIVED, RideStatus.CANCELLED),
    (RideStatus.IN_PROGRESS, RideStatus.COMPLETED),
}

# Order in which a normal ride moves through the statuses.
HAPPY_PATH = [
    RideStatus.REQUESTED,
    RideStatus.DRIVER_ASSIGNED,
    RideStatus.DRIVER_ARRIVED,
    RideStatus.IN_PROGRESS,
    RideStatus.COMPLETED,
]


async def make_ride(client, rider, driver, admin, target: RideStatus) -> int:
    """Creates a ride and walks it through the API until it reaches the target status."""
    response = await client.post("/rides", json=RIDE_BODY, headers=rider["headers"])
    ride_id = response.json()["id"]
    steps = [
        (f"/admin/rides/{ride_id}/assign", admin, {"driver_id": driver["driver"].id}),
        (f"/rides/{ride_id}/arrive", driver, None),
        (f"/rides/{ride_id}/start", driver, None),
        (f"/rides/{ride_id}/complete", driver, None),
    ]
    for url, actor, body in steps[: HAPPY_PATH.index(target)]:
        response = await client.post(url, json=body, headers=actor["headers"])
        assert response.status_code == 200
    return ride_id


@pytest.mark.parametrize(
    "from_status, to_status", list(itertools.product(RideStatus, RideStatus)), ids=lambda status: status.value
)
async def test_every_transition_pair(db, rider, from_status, to_status):
    ride = Ride(rider_id=rider["user"].id, status=from_status, **RIDE_BODY)
    db.add(ride)
    await db.commit()

    async def count_events() -> int:
        return await db.scalar(select(func.count()).select_from(RideEvent).where(RideEvent.ride_id == ride.id))

    if (from_status, to_status) in LEGAL_TRANSITIONS:
        await change_ride_status(db, ride, to_status, rider["user"].id)
        assert ride.status == to_status
        assert await count_events() == 1
    else:
        with pytest.raises(HTTPException) as error:
            await change_ride_status(db, ride, to_status, rider["user"].id)
        assert error.value.status_code == 409
        assert from_status.value in error.value.detail and to_status.value in error.value.detail
        assert ride.status == from_status
        assert await count_events() == 0


async def test_happy_path(client, rider, driver, admin):
    response = await client.post("/rides", json=RIDE_BODY, headers=rider["headers"])
    assert response.status_code == 201
    ride = response.json()
    ride_id = ride["id"]
    assert ride["status"] == "REQUESTED"
    assert ride["rider_id"] == rider["user"].id
    assert "otp" not in ride

    response = await client.post(
        f"/admin/rides/{ride_id}/assign", json={"driver_id": driver["driver"].id}, headers=admin["headers"]
    )
    assert response.status_code == 200
    assert response.json()["status"] == "DRIVER_ASSIGNED"
    assert response.json()["driver_id"] == driver["driver"].id

    response = await client.post(f"/rides/{ride_id}/arrive", headers=driver["headers"])
    assert response.json()["status"] == "DRIVER_ARRIVED"

    response = await client.post(f"/rides/{ride_id}/start", headers=driver["headers"])
    assert response.json()["status"] == "IN_PROGRESS"
    assert response.json()["started_at"] is not None
    assert response.json()["completed_at"] is None

    response = await client.post(f"/rides/{ride_id}/complete", headers=driver["headers"])
    assert response.json()["status"] == "COMPLETED"
    assert response.json()["completed_at"] is not None
    assert response.json()["final_fare"] is None

    response = await client.get(f"/rides/{ride_id}/events", headers=rider["headers"])
    events = [(event["from_status"], event["to_status"], event["actor_user_id"]) for event in response.json()]
    assert events == [
        (None, "REQUESTED", rider["user"].id),
        ("REQUESTED", "DRIVER_ASSIGNED", admin["user"].id),
        ("DRIVER_ASSIGNED", "DRIVER_ARRIVED", driver["user"].id),
        ("DRIVER_ARRIVED", "IN_PROGRESS", driver["user"].id),
        ("IN_PROGRESS", "COMPLETED", driver["user"].id),
    ]


@pytest.mark.parametrize("canceller", ["rider", "driver"])
@pytest.mark.parametrize(
    "ride_status",
    [RideStatus.REQUESTED, RideStatus.DRIVER_ASSIGNED, RideStatus.DRIVER_ARRIVED, RideStatus.IN_PROGRESS, RideStatus.COMPLETED],
    ids=lambda status: status.value,
)
async def test_cancel(client, rider, driver, admin, canceller, ride_status):
    ride_id = await make_ride(client, rider, driver, admin, ride_status)
    actor = rider if canceller == "rider" else driver

    response = await client.post(f"/rides/{ride_id}/cancel", headers=actor["headers"])

    if ride_status in (RideStatus.IN_PROGRESS, RideStatus.COMPLETED):
        assert response.status_code == 409
    elif canceller == "driver" and ride_status == RideStatus.REQUESTED:
        assert response.status_code == 404  # no driver is assigned yet, so the ride is not theirs
    else:
        assert response.status_code == 200
        assert response.json()["status"] == "CANCELLED"


async def test_second_active_ride_is_rejected_until_the_first_is_cancelled(client, rider):
    first = await client.post("/rides", json=RIDE_BODY, headers=rider["headers"])
    assert first.status_code == 201

    second = await client.post("/rides", json=RIDE_BODY, headers=rider["headers"])
    assert second.status_code == 409

    await client.post(f"/rides/{first.json()['id']}/cancel", headers=rider["headers"])
    third = await client.post("/rides", json=RIDE_BODY, headers=rider["headers"])
    assert third.status_code == 201


async def test_other_users_cannot_see_or_change_a_ride(client, rider, driver, admin, make_user):
    ride_id = await make_ride(client, rider, driver, admin, RideStatus.DRIVER_ASSIGNED)
    other_rider = await make_user("rider")
    other_driver = await make_user("driver")

    for url in (f"/rides/{ride_id}", f"/rides/{ride_id}/events"):
        assert (await client.get(url, headers=other_rider["headers"])).status_code == 404
        assert (await client.get(url, headers=other_driver["headers"])).status_code == 404
    assert (await client.post(f"/rides/{ride_id}/cancel", headers=other_rider["headers"])).status_code == 404
    assert (await client.post(f"/rides/{ride_id}/arrive", headers=other_driver["headers"])).status_code == 404
    assert (await client.get("/rides/99999", headers=rider["headers"])).status_code == 404

    assert (await client.post(f"/rides/{ride_id}/arrive", headers=rider["headers"])).status_code == 403
    assert (await client.get(f"/rides/{ride_id}", headers=admin["headers"])).status_code == 200

    # Nothing changed.
    assert (await client.get(f"/rides/{ride_id}", headers=rider["headers"])).json()["status"] == "DRIVER_ASSIGNED"


async def test_assign_rules(client, rider, driver, admin, make_user):
    ride_id = (await client.post("/rides", json=RIDE_BODY, headers=rider["headers"])).json()["id"]
    unapproved = await make_user("driver", approved=False)

    response = await client.post(
        f"/admin/rides/{ride_id}/assign", json={"driver_id": unapproved["driver"].id}, headers=admin["headers"]
    )
    assert response.status_code == 409

    response = await client.post(f"/admin/rides/{ride_id}/assign", json={"driver_id": 99999}, headers=admin["headers"])
    assert response.status_code == 404

    # The driver is busy with the first ride, so a second ride cannot be given to them.
    await client.post(f"/admin/rides/{ride_id}/assign", json={"driver_id": driver["driver"].id}, headers=admin["headers"])
    other_rider = await make_user("rider")
    second_ride_id = (await client.post("/rides", json=RIDE_BODY, headers=other_rider["headers"])).json()["id"]
    response = await client.post(
        f"/admin/rides/{second_ride_id}/assign", json={"driver_id": driver["driver"].id}, headers=admin["headers"]
    )
    assert response.status_code == 409

    # A ride that is no longer REQUESTED cannot be assigned.
    free_driver = await make_user("driver")
    await client.post(f"/rides/{second_ride_id}/cancel", headers=other_rider["headers"])
    response = await client.post(
        f"/admin/rides/{second_ride_id}/assign", json={"driver_id": free_driver["driver"].id}, headers=admin["headers"]
    )
    assert response.status_code == 409


async def test_two_simultaneous_cancels_only_one_wins(client, rider, db):
    ride_id = (await client.post("/rides", json=RIDE_BODY, headers=rider["headers"])).json()["id"]

    responses = await asyncio.gather(
        client.post(f"/rides/{ride_id}/cancel", headers=rider["headers"]),
        client.post(f"/rides/{ride_id}/cancel", headers=rider["headers"]),
    )

    assert sorted(response.status_code for response in responses) == [200, 409]
    cancelled_events = await db.scalar(
        select(func.count()).select_from(RideEvent).where(RideEvent.ride_id == ride_id, RideEvent.to_status == RideStatus.CANCELLED)
    )
    assert cancelled_events == 1


async def test_active_ride(client, rider, driver, admin):
    assert (await client.get("/rides/active", headers=rider["headers"])).status_code == 404
    assert (await client.get("/rides/active", headers=driver["headers"])).status_code == 404
    assert (await client.get("/rides/active", headers=admin["headers"])).status_code == 403

    ride_id = await make_ride(client, rider, driver, admin, RideStatus.IN_PROGRESS)
    for user in (rider, driver):
        response = await client.get("/rides/active", headers=user["headers"])
        assert response.status_code == 200
        assert response.json()["id"] == ride_id

    await client.post(f"/rides/{ride_id}/complete", headers=driver["headers"])
    assert (await client.get("/rides/active", headers=rider["headers"])).status_code == 404


@pytest.mark.parametrize(
    "changes",
    [
        {"dropoff_lat": RIDE_BODY["pickup_lat"], "dropoff_lng": RIDE_BODY["pickup_lng"]},
        {"pickup_lat": 91},
        {"pickup_lng": -181},
        {"pickup_address": "   "},
    ],
    ids=["same-place", "bad-latitude", "bad-longitude", "blank-address"],
)
async def test_invalid_ride_body_is_422(client, rider, changes):
    response = await client.post("/rides", json={**RIDE_BODY, **changes}, headers=rider["headers"])
    assert response.status_code == 422
