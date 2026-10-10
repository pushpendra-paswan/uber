import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import event, select, text

from app.config import settings
from app.database import Base, redis_client
from app.models import (
    Driver, Payment, PaymentMethod, PaymentStatus, Rating, Ride, RideEarning, RideEvent, RideStatus, User, UserRole, VerificationStatus,
)
from app.services import history as history_service
from app.services import ratings as ratings_service
from conftest import TEST_CHANNEL

RIDER_KEYS = {
    "id", "status", "created_at", "ended_at", "pickup_address", "dropoff_address", "distance_m", "duration_s", "final_fare",
    "payment_method", "driver_name", "cancelled_by", "has_receipt", "my_rating", "can_rate",
}
DRIVER_KEYS = {
    "id", "status", "created_at", "ended_at", "pickup_address", "dropoff_address", "distance_m", "duration_s", "fare",
    "payment_method", "platform_fee", "driver_earning", "cancelled_by", "my_rating", "can_rate",
}
ACTIVE = [RideStatus.REQUESTED, RideStatus.DRIVER_ASSIGNED, RideStatus.DRIVER_ARRIVED, RideStatus.IN_PROGRESS]
T0 = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)
LEGACY = {"kind": "legacy"}


# --- helpers ---


def cancellation(fee: int, by: str, reason: str = "late_cancellation") -> dict:
    return {"kind": "cancellation", "fee": fee, "reason": reason, "cancelled_by": by}


async def add_ride(
    db, rider: dict, driver: dict | None = None, status: RideStatus = RideStatus.COMPLETED, created_at: datetime = T0, *,
    final_fare: int | None = None, breakdown: dict | None = None, actual: tuple[int, int] | None = None, estimate: tuple[int, int] = (5000, 900),
    method: str = "cash", completed_at: datetime | None = None, events: list[tuple] | None = None, payment: int | None = None,
    earning: tuple[int, int] | None = None, otp: str | None = None,
) -> int:
    """Inserts a ride straight into the database. A COMPLETED ride is completed 25 minutes after the request unless
    completed_at says otherwise. events is a list of (from_status, to_status, time); by default there is one terminal
    event 30 minutes after the request (an active ride has none), and [] means no events at all. payment is the amount of a
    succeeded payment, and earning is (platform_fee, driver_earning) of its earning row. Returns the ride id."""
    if status == RideStatus.COMPLETED and completed_at is None:
        completed_at = created_at + timedelta(minutes=25)
    ride = Ride(
        rider_id=rider["user"].id, driver_id=driver["driver"].id if driver else None, status=status,
        pickup_lat=settings.city_center_lat, pickup_lng=settings.city_center_lng, pickup_address="MG Road",
        dropoff_lat=settings.city_south, dropoff_lng=settings.city_east, dropoff_address="Koramangala",
        distance_m=estimate[0], duration_s=estimate[1], fare_estimate=14000, final_fare=final_fare, fare_breakdown=breakdown,
        actual_distance_m=actual[0] if actual else None, actual_duration_s=actual[1] if actual else None,
        payment_method=PaymentMethod(method), completed_at=completed_at, created_at=created_at, otp=otp,
    )
    db.add(ride)
    await db.flush()
    if events is None:
        events = [] if status in ACTIVE else [(RideStatus.REQUESTED, status, created_at + timedelta(minutes=30))]
    for from_status, to_status, when in events:
        db.add(RideEvent(ride_id=ride.id, from_status=from_status, to_status=to_status, created_at=when))
    if payment is not None:
        row = Payment(
            ride_id=ride.id, amount=payment, method=PaymentMethod(method), status=PaymentStatus.succeeded,
            idempotency_key=f"ride:{ride.id}:charge", created_at=created_at,
        )
        db.add(row)
        await db.flush()
        if earning is not None:
            db.add(
                RideEarning(
                    ride_id=ride.id, payment_id=row.id, driver_id=driver["driver"].id,
                    kind="trip" if status == RideStatus.COMPLETED else "cancellation", gross_amount=payment, commission_percent=20,
                    platform_fee=earning[0], driver_earning=earning[1], created_at=created_at,
                )
            )
    await db.commit()
    return ride.id


async def add_rating(db, ride_id: int, from_user_id: int, to_user_id: int, score: int, comment: str | None = None) -> None:
    db.add(Rating(ride_id=ride_id, from_user_id=from_user_id, to_user_id=to_user_id, score=score, comment=comment))
    await db.commit()


async def history(client, who: dict, path: str = "/rides/history", **params):
    return await client.get(path, params=params, headers=who["headers"])


def ids_of(response) -> list[int]:
    assert response.status_code == 200, response.text
    return [row["id"] for row in response.json()]


def moment(text_value: str) -> datetime:
    return datetime.fromisoformat(text_value)


# --- 11. the rider's list ---


async def test_the_list_holds_only_the_callers_finished_rides_newest_first(client, db, rider, driver, make_user):
    stranger = await make_user("rider")
    first = await add_ride(db, rider, driver, RideStatus.COMPLETED)
    second = await add_ride(db, rider, driver, RideStatus.CANCELLED, breakdown=cancellation(0, "rider"), final_fare=0)
    third = await add_ride(db, rider, None, RideStatus.NO_DRIVER_FOUND)
    await add_ride(db, stranger, driver, RideStatus.COMPLETED)

    response = await history(client, rider)

    assert ids_of(response) == [third, second, first]
    assert all(set(row) == RIDER_KEYS for row in response.json())
    assert len(ids_of(await history(client, stranger))) == 1


@pytest.mark.parametrize("status", ACTIVE, ids=lambda status: status.value)
async def test_an_active_ride_never_appears(client, db, make_user, driver, status):
    busy = await make_user("rider")
    finished = await add_ride(db, busy, driver, RideStatus.COMPLETED)
    await add_ride(db, busy, driver if status != RideStatus.REQUESTED else None, status)

    assert ids_of(await history(client, busy)) == [finished]


async def test_the_order_is_by_ride_id_even_when_request_times_disagree(client, db, rider, driver):
    older_id_newer_time = await add_ride(db, rider, driver, created_at=T0 + timedelta(days=5))
    newer_id_older_time = await add_ride(db, rider, driver, created_at=T0)

    assert ids_of(await history(client, rider)) == [newer_id_older_time, older_id_newer_time]


async def test_the_status_filter_works_alone_and_with_a_window(client, db, rider, driver):
    done = await add_ride(db, rider, driver, RideStatus.COMPLETED, T0)
    cancelled = await add_ride(db, rider, driver, RideStatus.CANCELLED, T0 + timedelta(days=1), breakdown=cancellation(0, "rider"), final_fare=0)
    nobody = await add_ride(db, rider, None, RideStatus.NO_DRIVER_FOUND, T0 + timedelta(days=2))
    done_later = await add_ride(db, rider, driver, RideStatus.COMPLETED, T0 + timedelta(days=3))

    assert ids_of(await history(client, rider, status="COMPLETED")) == [done_later, done]
    assert ids_of(await history(client, rider, status="CANCELLED")) == [cancelled]
    assert ids_of(await history(client, rider, status="NO_DRIVER_FOUND")) == [nobody]
    window = {"since": (T0 + timedelta(days=1)).isoformat(), "until": (T0 + timedelta(days=3)).isoformat()}
    assert ids_of(await history(client, rider, status="COMPLETED", **window)) == []
    assert ids_of(await history(client, rider, status="CANCELLED", **window)) == [cancelled]


@pytest.mark.parametrize("status", ["REQUESTED", "DRIVER_ASSIGNED", "DRIVER_ARRIVED", "IN_PROGRESS"])
async def test_an_active_status_filter_is_a_422(client, rider, status):
    response = await history(client, rider, status=status)

    assert response.status_code == 422
    assert response.json()["detail"] == "Only finished trips are listed"


async def test_an_unknown_status_is_a_422(client, rider):
    assert (await history(client, rider, status="TELEPORTED")).status_code == 422


@pytest.mark.parametrize("limit", ["0", "-1", "51", "abc", "1.5", ""])
async def test_a_bad_limit_is_a_422(client, rider, limit):
    assert (await history(client, rider, limit=limit)).status_code == 422


async def test_the_limits_themselves_are_accepted(client, db, rider, driver):
    for _ in range(3):
        await add_ride(db, rider, driver)

    assert len(ids_of(await history(client, rider, limit=1))) == 1
    assert len(ids_of(await history(client, rider, limit=50))) == 3
    assert (history_service.HISTORY_DEFAULT_LIMIT, history_service.HISTORY_MAX_LIMIT) == (20, 50)


async def test_paging_over_60_rides_returns_every_ride_once(client, db, rider, driver):
    expected = [await add_ride(db, rider, driver, created_at=T0 + timedelta(minutes=minute)) for minute in range(60)][::-1]

    seen, before_id, pages = [], None, 0
    for _ in range(10):  # bounded: a page that repeats its last row must fail the test, not loop for ever
        params = {"limit": 25} | ({"before_id": before_id} if before_id else {})
        page = ids_of(await history(client, rider, **params))
        if not page:
            break
        assert len(page) <= 25
        seen += page
        before_id = page[-1]
        pages += 1
    else:
        pytest.fail("the pages never ran out")

    assert (seen, pages) == (expected, 3)  # 25 + 25 + 10: every row once, in order, no gaps
    # The default page is 20 rows, and before_id is exclusive.
    assert ids_of(await history(client, rider)) == expected[:20]
    assert ids_of(await history(client, rider, before_id=expected[0])) == expected[1:21]


async def test_since_is_inclusive_and_until_is_exclusive(client, db, rider, driver):
    early = await add_ride(db, rider, driver, created_at=T0 - timedelta(seconds=1))
    on_since = await add_ride(db, rider, driver, created_at=T0)
    inside = await add_ride(db, rider, driver, created_at=T0 + timedelta(hours=1))
    on_until = await add_ride(db, rider, driver, created_at=T0 + timedelta(hours=2))

    window = {"since": T0.isoformat(), "until": (T0 + timedelta(hours=2)).isoformat()}

    assert ids_of(await history(client, rider, **window)) == [inside, on_since]
    assert ids_of(await history(client, rider, since=T0.isoformat())) == [on_until, inside, on_since]
    assert ids_of(await history(client, rider, until=T0.isoformat())) == [early]
    # The same instant written with a Z and with an offset.
    assert ids_of(await history(client, rider, since="2026-03-01T12:00:00Z", until="2026-03-01T19:30:00+05:30")) == [inside, on_since]


@pytest.mark.parametrize(
    "params",
    [
        {"since": "2026-03-01T12:00:00"}, {"until": "2026-03-01T12:00:00"}, {"since": "yesterday"},
        {"since": "2026-03-01T12:00:00Z", "until": "2026-03-01T12:00:00Z"},
        {"since": "2026-03-02T12:00:00Z", "until": "2026-03-01T12:00:00Z"},
    ],
    ids=str,
)
async def test_a_naive_time_and_a_window_that_ends_before_it_starts_are_422(client, rider, params):
    assert (await history(client, rider, **params)).status_code == 422


async def test_until_not_after_since_says_so(client, rider):
    response = await history(client, rider, since="2026-03-02T00:00:00Z", until="2026-03-01T00:00:00Z")

    assert response.json()["detail"] == "until must be after since"


# --- 12. field semantics, worked out by hand ---


async def test_distance_and_time_are_the_tracked_values_or_else_the_estimate(client, db, rider, driver):
    tracked = await add_ride(db, rider, driver, estimate=(5000, 900), actual=(5230, 840), final_fare=15300)
    untracked = await add_ride(db, rider, driver, estimate=(4800, 700), final_fare=12000)

    rows = {row["id"]: row for row in (await history(client, rider)).json()}

    assert (rows[tracked]["distance_m"], rows[tracked]["duration_s"]) == (5230, 840)  # actual_* wins over the estimate
    assert (rows[untracked]["distance_m"], rows[untracked]["duration_s"]) == (4800, 700)  # no actual_*: the estimate


async def test_a_trip_row_is_worked_out_field_by_field(client, db, rider, driver):
    events = [
        (None, RideStatus.REQUESTED, T0),
        (RideStatus.REQUESTED, RideStatus.DRIVER_ASSIGNED, T0 + timedelta(minutes=2)),
        (RideStatus.DRIVER_ASSIGNED, RideStatus.DRIVER_ARRIVED, T0 + timedelta(minutes=9)),
        (RideStatus.DRIVER_ARRIVED, RideStatus.IN_PROGRESS, T0 + timedelta(minutes=11)),
        (RideStatus.IN_PROGRESS, RideStatus.COMPLETED, T0 + timedelta(minutes=26, seconds=30)),
    ]
    ride_id = await add_ride(
        db, rider, driver, RideStatus.COMPLETED, T0, final_fare=15300, actual=(5230, 840), method="wallet", payment=15300,
        earning=(3060, 12240), events=events, breakdown={"kind": "trip"},
    )

    row = (await history(client, rider)).json()[0]

    assert moment(row.pop("created_at")) == T0
    assert moment(row.pop("ended_at")) == T0 + timedelta(minutes=26, seconds=30)  # the LAST event, not the first
    assert row == {
        "id": ride_id, "status": "COMPLETED", "pickup_address": "MG Road", "dropoff_address": "Koramangala", "distance_m": 5230,
        "duration_s": 840, "final_fare": 15300, "payment_method": "wallet", "driver_name": "Test driver", "cancelled_by": None,
        "has_receipt": True, "my_rating": None, "can_rate": False,  # can_rate: completed in March 2026, long past the window
    }


async def test_final_fare_is_the_stored_value(client, db, rider, driver):
    fee = await add_ride(db, rider, driver, RideStatus.CANCELLED, final_fare=3000, breakdown=cancellation(3000, "rider"), payment=3000)
    free = await add_ride(db, rider, driver, RideStatus.CANCELLED, final_fare=0, breakdown=cancellation(0, "rider", "within_free_window"))
    nobody = await add_ride(db, rider, None, RideStatus.NO_DRIVER_FOUND)
    legacy = await add_ride(db, rider, driver, RideStatus.COMPLETED, breakdown=LEGACY)

    rows = {row["id"]: row for row in (await history(client, rider)).json()}

    assert rows[fee]["final_fare"] == 3000
    assert rows[free]["final_fare"] == 0  # a free cancellation is 0, not null
    assert rows[nobody]["final_fare"] is None
    assert rows[legacy]["final_fare"] is None


async def test_has_receipt_is_true_only_when_a_payment_row_exists(client, db, rider, driver):
    paid = await add_ride(db, rider, driver, final_fare=14000, payment=14000)
    unpaid = await add_ride(db, rider, driver, RideStatus.CANCELLED, final_fare=0, breakdown=cancellation(0, "driver", "driver_cancelled"))
    cash = await add_ride(db, rider, driver, final_fare=9000, method="cash", payment=9000)

    rows = {row["id"]: row for row in (await history(client, rider)).json()}

    assert (rows[paid]["has_receipt"], rows[unpaid]["has_receipt"], rows[cash]["has_receipt"]) == (True, False, True)
    assert (rows[paid]["payment_method"], rows[cash]["payment_method"]) == ("cash", "cash")


async def test_driver_name_is_the_drivers_name_or_null_when_nobody_was_assigned(client, db, rider, driver):
    assigned = await add_ride(db, rider, driver, RideStatus.CANCELLED, final_fare=0, breakdown=cancellation(0, "rider"))
    before_assignment = await add_ride(db, rider, None, RideStatus.CANCELLED, final_fare=0, breakdown=cancellation(0, "rider", "no_driver_yet"))
    nobody = await add_ride(db, rider, None, RideStatus.NO_DRIVER_FOUND)

    rows = {row["id"]: row for row in (await history(client, rider)).json()}

    assert rows[assigned]["driver_name"] == driver["user"].name == "Test driver"
    assert (rows[before_assignment]["driver_name"], rows[nobody]["driver_name"]) == (None, None)


async def test_ended_at_is_the_last_event_and_null_without_events(client, db, rider, driver):
    last = T0 + timedelta(hours=3, minutes=7)
    events = [(None, RideStatus.REQUESTED, T0), (RideStatus.REQUESTED, RideStatus.CANCELLED, last)]
    with_events = await add_ride(db, rider, driver, RideStatus.CANCELLED, events=events, final_fare=0, breakdown=cancellation(0, "rider"))
    without = await add_ride(db, rider, driver, RideStatus.COMPLETED, events=[])

    rows = {row["id"]: row for row in (await history(client, rider)).json()}

    assert moment(rows[with_events]["ended_at"]) == last
    assert rows[without]["ended_at"] is None


async def test_cancelled_by_is_set_for_real_cancellations_and_null_for_everything_else(client, db, rider, driver, assign_ride):
    # Real cancellations, through the API: first by the rider, then by the driver.
    first = await assign_ride(rider, driver)
    assert (await client.post(f"/rides/{first['id']}/cancel", headers=rider["headers"])).status_code == 200
    second = await assign_ride(rider, driver)
    assert (await client.post(f"/rides/{second['id']}/cancel", headers=driver["headers"])).status_code == 200
    completed = await add_ride(db, rider, driver, RideStatus.COMPLETED, breakdown={"kind": "trip"})
    legacy = await add_ride(db, rider, driver, RideStatus.CANCELLED, final_fare=None, breakdown=LEGACY)

    rows = {row["id"]: row for row in (await history(client, rider)).json()}
    driver_rows = {row["id"]: row for row in (await history(client, driver, "/drivers/me/history")).json()}

    assert rows[first["id"]]["cancelled_by"] == "rider"
    assert rows[second["id"]]["cancelled_by"] == "driver"
    assert (rows[completed]["cancelled_by"], rows[legacy]["cancelled_by"]) == (None, None)
    assert (driver_rows[first["id"]]["cancelled_by"], driver_rows[second["id"]]["cancelled_by"]) == ("rider", "driver")
    assert all(row["status"] == "CANCELLED" for row in (rows[first["id"]], rows[second["id"]]))
    assert rows[second["id"]]["final_fare"] == 0  # a driver never causes a fee


# --- 13. ratings in the rows ---


async def test_my_rating_is_only_the_callers_own_score(client, db, rider, driver):
    ride_id = await add_ride(db, rider, driver, final_fare=14000, payment=14000)
    await add_rating(db, ride_id, rider["user"].id, driver["user"].id, 4, "COMMENT-FROM-THE-RIDER")
    await add_rating(db, ride_id, driver["user"].id, rider["user"].id, 1, "COMMENT-FROM-THE-DRIVER")

    rider_row = (await history(client, rider)).json()[0]
    driver_row = (await history(client, driver, "/drivers/me/history")).json()[0]

    assert (rider_row["my_rating"], driver_row["my_rating"]) == (4, 1)


async def test_can_rate_follows_the_state_of_the_ride(client, db, rider, driver):
    now = datetime.now(timezone.utc)
    fresh = await add_ride(db, rider, driver, created_at=now - timedelta(hours=2), completed_at=now - timedelta(hours=1))
    rated = await add_ride(db, rider, driver, created_at=now - timedelta(hours=2), completed_at=now - timedelta(hours=1))
    await add_rating(db, rated, rider["user"].id, driver["user"].id, 5)
    cancelled = await add_ride(db, rider, driver, RideStatus.CANCELLED, now - timedelta(hours=2), final_fare=0, breakdown=cancellation(0, "rider"))
    old = await add_ride(db, rider, driver, created_at=now - timedelta(days=9), completed_at=now - timedelta(days=8))
    no_time = await add_ride(db, rider, driver, created_at=now - timedelta(hours=2))
    await db.execute(text("UPDATE rides SET completed_at = NULL WHERE id = :id"), {"id": no_time})
    await db.commit()

    rows = {row["id"]: row for row in (await history(client, rider)).json()}

    assert rows[fresh]["can_rate"] is True
    assert rows[rated]["can_rate"] is False  # already rated by this person
    assert rows[cancelled]["can_rate"] is False  # only COMPLETED trips can be rated
    assert rows[old]["can_rate"] is False  # completed 8 days ago, the window is 7
    assert rows[no_time]["can_rate"] is False  # no completed_at: never rateable
    assert rows[rated]["my_rating"] == 5 and rows[fresh]["my_rating"] is None


async def test_the_other_sides_rating_does_not_stop_me_from_rating(client, db, rider, driver):
    now = datetime.now(timezone.utc)
    ride_id = await add_ride(db, rider, driver, created_at=now - timedelta(hours=2), completed_at=now - timedelta(hours=1))
    await add_rating(db, ride_id, driver["user"].id, rider["user"].id, 2)

    rider_row = (await history(client, rider)).json()[0]
    driver_row = (await history(client, driver, "/drivers/me/history")).json()[0]

    assert (rider_row["my_rating"], rider_row["can_rate"]) == (None, True)
    assert (driver_row["my_rating"], driver_row["can_rate"]) == (2, False)


class FixedDatetime(datetime):
    """A datetime whose now() is a fixed instant, so 'exactly at the end of the window' can be tested."""

    @classmethod
    def now(cls, tz=None):
        return NOW


NOW = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)


@pytest.mark.parametrize(
    "window_days, age",
    [
        (7, timedelta(days=6, hours=23)), (7, timedelta(days=7)), (7, timedelta(days=7, hours=1)), (7, timedelta(days=7, microseconds=1)),
        (2, timedelta(days=1, hours=23)), (2, timedelta(days=2)), (2, timedelta(days=2, hours=1)),
    ],
    ids=str,
)
async def test_can_rate_agrees_with_the_rating_endpoint_around_the_end_of_the_window(client, db, rider, driver, monkeypatch, window_days, age):
    monkeypatch.setattr(ratings_service, "RATING_WINDOW_DAYS", window_days)
    monkeypatch.setattr(ratings_service, "datetime", FixedDatetime)
    monkeypatch.setattr(history_service, "datetime", FixedDatetime)
    ride_id = await add_ride(db, rider, driver, created_at=NOW - age - timedelta(hours=1), completed_at=NOW - age)

    inside_the_window = age <= timedelta(days=window_days)  # exactly at the end of the window is still allowed
    for who, path in ((rider, "/rides/history"), (driver, "/drivers/me/history")):
        row = (await history(client, who, path)).json()[0]
        rated = await client.post(f"/rides/{ride_id}/rating", json={"score": 5}, headers=who["headers"])
        assert row["can_rate"] == inside_the_window == (rated.status_code == 201), (path, age, rated.text)
        assert rated.status_code in (201, 409)
        # And once rated, the row says so and no longer offers the rating.
        after = (await history(client, who, path)).json()[0]
        assert (after["can_rate"], after["my_rating"]) == (False, 5 if rated.status_code == 201 else None)


# --- 14. privacy ---


async def test_neither_comment_nor_the_other_sides_score_appears_in_any_response(client, db, rider, driver):
    now = datetime.now(timezone.utc)
    ride_id = await add_ride(
        db, rider, driver, created_at=now - timedelta(hours=2), completed_at=now - timedelta(hours=1), final_fare=14000, payment=14000,
        earning=(2800, 11200), otp="9137",
    )
    await add_rating(db, ride_id, rider["user"].id, driver["user"].id, 4, "COMMENT-FROM-THE-RIDER-77")
    await add_rating(db, ride_id, driver["user"].id, rider["user"].id, 2, "COMMENT-FROM-THE-DRIVER-88")

    rider_text = (await history(client, rider)).text
    driver_text = (await history(client, driver, "/drivers/me/history")).text

    for text_value in (rider_text, driver_text):
        assert "COMMENT-FROM" not in text_value
        assert "comment" not in text_value
        assert "otp" not in text_value.lower()
        assert "9137" not in text_value
    # The other side's score is not in the row either: the rider's row has only the rider's own 4, the driver's only the 2.
    assert json.loads(rider_text)[0]["my_rating"] == 4
    assert json.loads(driver_text)[0]["my_rating"] == 2


async def test_a_riders_rows_hold_nothing_about_the_driver_but_the_name(client, db, rider, driver):
    await db.execute(text("UPDATE users SET phone = '+919876543210' WHERE id = :id"), {"id": driver["user"].id})
    await db.commit()
    await add_ride(db, rider, driver, final_fare=14000, payment=14000, earning=(2800, 11200))

    response = await history(client, rider)
    row_text = response.text

    assert set(response.json()[0]) == RIDER_KEYS
    for secret in (
        driver["user"].email, "+919876543210", driver["driver"].license_number, "PLATE", "Swift", "white", "commission", "platform_fee",
        "driver_earning", "driver_id", "rider_id", "email", "phone", "license", "vehicle", "earning", "otp",
    ):
        assert secret not in row_text, secret
    assert json.loads(row_text)[0]["driver_name"] == "Test driver"


async def test_another_riders_rides_never_appear(client, db, rider, driver, make_user):
    stranger = await make_user("rider")
    theirs = await add_ride(db, stranger, driver)
    mine = await add_ride(db, rider, driver)

    assert ids_of(await history(client, rider)) == [mine]
    assert ids_of(await history(client, rider, before_id=mine + 5)) == [mine]
    assert theirs not in ids_of(await history(client, rider, limit=50))


# --- 15. the driver's list ---


async def test_the_driver_list_holds_only_their_own_completed_and_cancelled_rides(client, db, rider, driver, make_user):
    other_driver = await make_user("driver")
    other_rider = await make_user("rider")
    done = await add_ride(db, rider, driver, RideStatus.COMPLETED, T0, final_fare=14000, payment=14000, earning=(2800, 11200))
    cancelled = await add_ride(db, other_rider, driver, RideStatus.CANCELLED, T0 + timedelta(days=1), final_fare=0, breakdown=cancellation(0, "driver", "driver_cancelled"))
    await add_ride(db, rider, other_driver, RideStatus.COMPLETED, T0 + timedelta(days=2))  # another driver's ride
    await add_ride(db, rider, None, RideStatus.NO_DRIVER_FOUND, T0 + timedelta(days=3))  # never has a driver
    await add_ride(db, other_rider, driver, RideStatus.IN_PROGRESS, T0 + timedelta(days=4))  # active: not listed

    response = await history(client, driver, "/drivers/me/history")

    assert ids_of(response) == [cancelled, done]
    assert all(set(row) == DRIVER_KEYS for row in response.json())
    assert ids_of(await history(client, driver, "/drivers/me/history", status="COMPLETED")) == [done]
    assert ids_of(await history(client, driver, "/drivers/me/history", status="CANCELLED")) == [cancelled]
    for status in ("NO_DRIVER_FOUND", "REQUESTED", "IN_PROGRESS"):
        refused = await history(client, driver, "/drivers/me/history", status=status)
        assert (refused.status_code, refused.json()["detail"]) == (422, "Only finished trips are listed")


async def test_the_driver_sees_the_stored_fare_and_the_earning_rows_split(client, db, rider, driver):
    split = await add_ride(db, rider, driver, final_fare=15300, actual=(5230, 840), method="wallet", payment=15300, earning=(3060, 12240))
    no_earning = await add_ride(db, rider, driver, RideStatus.COMPLETED, final_fare=None, estimate=(4800, 700), breakdown=LEGACY)
    free = await add_ride(db, rider, driver, RideStatus.CANCELLED, final_fare=0, breakdown=cancellation(0, "driver", "driver_cancelled"))

    rows = {row["id"]: row for row in (await history(client, driver, "/drivers/me/history")).json()}

    assert (rows[split]["fare"], rows[split]["platform_fee"], rows[split]["driver_earning"]) == (15300, 3060, 12240)
    assert (rows[split]["payment_method"], rows[split]["distance_m"], rows[split]["duration_s"]) == ("wallet", 5230, 840)
    assert (rows[no_earning]["fare"], rows[no_earning]["platform_fee"], rows[no_earning]["driver_earning"]) == (None, None, None)
    assert (rows[no_earning]["distance_m"], rows[no_earning]["duration_s"]) == (4800, 700)
    assert (rows[free]["fare"], rows[free]["platform_fee"], rows[free]["driver_earning"]) == (0, None, None)


async def test_a_driver_row_holds_nothing_about_the_rider(client, db, rider, driver):
    now = datetime.now(timezone.utc)
    await db.execute(
        text("UPDATE users SET name = 'Zelda Rideson', phone = '+911234567890' WHERE id = :id"), {"id": rider["user"].id}
    )
    await db.commit()
    ride_id = await add_ride(
        db, rider, driver, created_at=now - timedelta(hours=2), completed_at=now - timedelta(hours=1), final_fare=14000, payment=14000,
        earning=(2800, 11200), otp="9137",
    )
    await add_rating(db, ride_id, rider["user"].id, driver["user"].id, 5, "RIDER-COMMENT-55")
    await add_rating(db, ride_id, driver["user"].id, rider["user"].id, 3, "DRIVER-COMMENT-66")

    response = await history(client, driver, "/drivers/me/history")
    row_text = response.text

    assert set(response.json()[0]) == DRIVER_KEYS
    for secret in (
        "Zelda", "Rideson", rider["user"].email, "+911234567890", "rider", "RIDER-COMMENT", "DRIVER-COMMENT", "comment", "otp", "9137",
        "name", "email", "phone",
    ):
        assert secret not in row_text, secret
    assert json.loads(row_text)[0]["my_rating"] == 3  # only the driver's own


async def test_the_driver_list_pages_and_windows_like_the_rider_list(client, db, rider, driver):
    expected = [await add_ride(db, rider, driver, created_at=T0 + timedelta(hours=hour)) for hour in range(30)][::-1]
    path = "/drivers/me/history"

    seen, before_id = [], None
    for _ in range(10):
        page = ids_of(await history(client, driver, path, limit=7, **({"before_id": before_id} if before_id else {})))
        if not page:
            break
        seen += page
        before_id = page[-1]
    else:
        pytest.fail("the pages never ran out")
    window = {"since": (T0 + timedelta(hours=5)).isoformat(), "until": (T0 + timedelta(hours=8)).isoformat()}

    assert seen == expected
    assert len(ids_of(await history(client, driver, path))) == 20
    assert ids_of(await history(client, driver, path, **window)) == [expected[29 - 7], expected[29 - 6], expected[29 - 5]]  # hours 7, 6, 5
    for limit in ("0", "51", "abc"):
        assert (await history(client, driver, path, limit=limit)).status_code == 422
    assert (await history(client, driver, path, since="2026-03-01T12:00:00")).status_code == 422
    assert (await history(client, driver, path, since="2026-03-02T00:00:00Z", until="2026-03-01T00:00:00Z")).status_code == 422


# --- 16. access ---


@pytest.mark.parametrize("path, allowed", [("/rides/history", "rider"), ("/drivers/me/history", "driver")])
async def test_each_list_belongs_to_one_role(client, rider, driver, admin, path, allowed):
    people = {"rider": rider, "driver": driver}
    assert (await history(client, people[allowed], path)).status_code == 200
    for name, who in {**people, "admin": admin}.items():
        if name != allowed:
            assert (await history(client, who, path)).status_code == 403
    assert (await client.get(path)).status_code == 401


async def test_a_driver_without_a_profile_gets_a_404(client, db):
    from app.security import create_access_token

    user = User(role=UserRole.driver, name="No profile", email="noprofile@example.com", password_hash="unused")
    db.add(user)
    await db.commit()

    response = await client.get("/drivers/me/history", headers={"Authorization": f"Bearer {create_access_token(user)}"})

    assert (response.status_code, response.json()["detail"]) == (404, "You have no driver profile yet")


async def test_a_rejected_or_pending_driver_may_read_their_own_history(client, db, rider, driver):
    ride_id = await add_ride(db, rider, driver)
    for status in (VerificationStatus.rejected, VerificationStatus.pending):
        await db.execute(Driver.__table__.update().where(Driver.id == driver["driver"].id).values(verification_status=status))
        await db.commit()
        assert ids_of(await history(client, driver, "/drivers/me/history")) == [ride_id]


# --- 17. routing ---


async def test_history_is_not_captured_by_the_ride_id_route(client, db, rider, driver):
    ride_id = await add_ride(db, rider, driver)

    listed = await history(client, rider)
    active = await client.get("/rides/active", headers=rider["headers"])
    one = await client.get(f"/rides/{ride_id}", headers=rider["headers"])

    assert (listed.status_code, [row["id"] for row in listed.json()]) == (200, [ride_id])  # a list, not a 422 from /{ride_id}
    assert (active.status_code, active.json()["detail"]) == (404, "No active ride")
    assert (one.status_code, one.json()["id"]) == (200, ride_id)


# --- 18. no N+1 ---


@pytest.fixture
def statements(test_engine):
    """Every SQL statement the test engine runs, in order."""
    seen = []

    def record(connection, cursor, statement, parameters, context, executemany):
        seen.append(statement)

    event.listen(test_engine.sync_engine, "before_cursor_execute", record)
    yield seen
    event.remove(test_engine.sync_engine, "before_cursor_execute", record)


async def test_a_page_costs_the_same_number_of_statements_whatever_its_size(client, db, rider, driver, statements):
    for index in range(50):
        await add_ride(
            db, rider, driver, created_at=T0 + timedelta(minutes=index), final_fare=14000, payment=14000, earning=(2800, 11200)
        )
    counts = {}

    for name, who, path in (("rider", rider, "/rides/history"), ("driver", driver, "/drivers/me/history")):
        for size in (5, 50):
            statements.clear()
            response = await history(client, who, path, limit=size)
            assert len(response.json()) == size
            counts[name, size] = len(statements)

    assert counts["rider", 5] == counts["rider", 50] == 2  # the caller's user row, and the one query for the page
    assert counts["driver", 5] == counts["driver", 50] == 5  # the user row, get_me (driver, user, vehicle), and the one query
    history_statements = [statement for statement in statements if "FROM rides" in statement]
    assert len(history_statements) == 1


# --- 19. quiet side effects ---


async def test_a_history_request_writes_nothing_and_publishes_nothing(client, db, rider, driver):
    ride_id = await add_ride(db, rider, driver, final_fare=14000, payment=14000, earning=(2800, 11200))
    await add_rating(db, ride_id, rider["user"].id, driver["user"].id, 5)

    async def snapshot() -> dict:
        tables = {}
        for table in Base.metadata.sorted_tables:
            rows = (await db.execute(text(f'SELECT * FROM "{table.name}" ORDER BY 1'))).all()
            tables[table.name] = [tuple(map(str, row)) for row in rows]
        return tables

    before = await snapshot()
    pubsub = redis_client.pubsub()
    await pubsub.subscribe(TEST_CHANNEL)
    assert (await pubsub.get_message(timeout=2))["type"] == "subscribe"

    for who, path in ((rider, "/rides/history"), (driver, "/drivers/me/history")):
        assert (await history(client, who, path)).status_code == 200
        assert (await history(client, who, path, status="COMPLETED", limit=1)).status_code == 200
    await asyncio.sleep(0.2)

    assert await snapshot() == before
    assert await pubsub.get_message(timeout=0.3, ignore_subscribe_messages=True) is None
    await pubsub.aclose()
