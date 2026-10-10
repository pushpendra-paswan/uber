from datetime import datetime, timedelta, timezone

import pytest
from redis.exceptions import RedisError
from sqlalchemy import text

from app.models import OfferStatus, Ride, RideEvent, RideOffer, RideStatus
from app.repositories import drivers as drivers_repo
from app.services import earnings as earnings_service
from test_edge_cases import logged  # noqa: F401  (a fixture: caplog attached to uvicorn's logger)

T0 = datetime(2026, 3, 1, 0, 0, tzinfo=timezone.utc)
DAY = timedelta(days=1)
HOUR = timedelta(hours=1)
LAT, LNG = 12.9716, 77.5946


async def get_stats(client, admin: dict, since: datetime, until: datetime, bucket: str = "day", offset: int = 0):
    params = {"since": since.isoformat(), "until": until.isoformat(), "bucket": bucket, "utc_offset_minutes": offset}
    return await client.get("/admin/stats", params=params, headers=admin["headers"])


async def stats_of(client, admin: dict, since: datetime, until: datetime, **options) -> dict:
    response = await get_stats(client, admin, since, until, **options)
    assert response.status_code == 200, response.text
    return response.json()


async def add_ride(db, rider: dict, status: RideStatus, created_at: datetime, driver: dict | None = None, **extra) -> Ride:
    ride = Ride(
        rider_id=rider["user"].id, driver_id=driver["driver"].id if driver else None, status=status, created_at=created_at,
        pickup_lat=LAT, pickup_lng=LNG, pickup_address="MG Road", dropoff_lat=LAT + 0.01, dropoff_lng=LNG + 0.01,
        dropoff_address="Koramangala", distance_m=5000, duration_s=900, fare_estimate=14000, **extra,
    )
    db.add(ride)
    await db.commit()
    return ride


async def add_many(db, make_user, counts: dict, created_at: datetime) -> None:
    """Rides in the given statuses at one time. Finished rides share a rider; each active ride needs its own (one active ride
    per rider)."""
    shared = await make_user("rider")
    for status, number in counts.items():
        for _ in range(number):
            active = status in (RideStatus.REQUESTED, RideStatus.DRIVER_ASSIGNED, RideStatus.DRIVER_ARRIVED, RideStatus.IN_PROGRESS)
            await add_ride(db, await make_user("rider") if active else shared, status, created_at)


def epoch(*args) -> int:
    return int(datetime(*args, tzinfo=timezone.utc).timestamp())


# --- 18. buckets ---


async def test_hour_buckets_with_an_offset_of_330_minutes_split_at_half_past(client, db, admin, rider):
    # Local time is UTC+5:30, so a local hour starts at HH:30 UTC. Rides just before and just after two boundaries.
    for moment in ((0, 29, 59), (0, 30, 0), (1, 29, 59), (1, 30, 0)):
        await add_ride(db, rider, RideStatus.COMPLETED, datetime(2026, 3, 1, *moment, tzinfo=timezone.utc))

    body = await stats_of(client, admin, T0, T0 + 6 * HOUR, bucket="hour", offset=330)

    series = body["series"]
    # The first bucket is the local hour that contains 00:00 UTC (it began at 23:30 UTC the day before), the last the one that
    # contains 05:59:59 UTC (it began at 05:30 UTC).
    assert [entry["start"] for entry in series] == [epoch(2026, 2, 28, 23, 30) + 3600 * index for index in range(7)]
    assert [entry["rides"] for entry in series] == [1, 2, 1, 0, 0, 0, 0]
    assert [entry["completed"] for entry in series] == [1, 2, 1, 0, 0, 0, 0]
    assert series[0]["start"] <= int(T0.timestamp()) and series[-1]["start"] <= int((T0 + 6 * HOUR).timestamp()) - 1 < series[-1]["start"] + 3600


async def test_day_buckets_split_at_local_midnight(client, db, admin, rider):
    # Local midnight at UTC+5:30 is 18:30 UTC.
    for moment in ((1, 18, 29, 59), (1, 18, 30, 0)):
        await add_ride(db, rider, RideStatus.CANCELLED, datetime(2026, 3, *moment, tzinfo=timezone.utc))

    body = await stats_of(client, admin, T0, T0 + 3 * DAY, bucket="day", offset=330)

    series = body["series"]
    assert [entry["start"] for entry in series] == [epoch(2026, 2, 28, 18, 30), epoch(2026, 3, 1, 18, 30), epoch(2026, 3, 2, 18, 30), epoch(2026, 3, 3, 18, 30)]
    assert [entry["rides"] for entry in series] == [1, 1, 0, 0]
    assert [entry["completed"] for entry in series] == [0, 0, 0, 0]


async def test_a_negative_offset_moves_the_boundary_the_other_way(client, db, admin, rider):
    # UTC-5: local midnight is 05:00 UTC.
    for moment in ((4, 59, 59), (5, 0, 0)):
        await add_ride(db, rider, RideStatus.COMPLETED, datetime(2026, 3, 1, *moment, tzinfo=timezone.utc))

    body = await stats_of(client, admin, T0, T0 + DAY, bucket="day", offset=-300)

    assert [entry["start"] for entry in body["series"]] == [epoch(2026, 2, 28, 5), epoch(2026, 3, 1, 5)]
    assert [entry["rides"] for entry in body["series"]] == [1, 1]


async def test_an_offset_of_zero_aligns_buckets_with_utc_and_the_series_is_zero_filled(client, db, admin, rider):
    await add_ride(db, rider, RideStatus.COMPLETED, T0 + 5 * HOUR + timedelta(minutes=10))

    body = await stats_of(client, admin, T0, T0 + DAY, bucket="hour", offset=0)

    series = body["series"]
    assert len(series) == 24
    assert [entry["start"] for entry in series] == [int(T0.timestamp()) + 3600 * index for index in range(24)]
    assert [index for index, entry in enumerate(series) if entry["rides"]] == [5]
    assert all(entry["gross"] == 0 and entry["platform_fee"] == 0 for entry in series)


async def test_a_window_ending_inside_a_bucket_includes_that_bucket(client, admin):
    body = await stats_of(client, admin, T0 + timedelta(minutes=30), T0 + 2 * HOUR + timedelta(minutes=15), bucket="hour")

    assert [entry["start"] for entry in body["series"]] == [epoch(2026, 3, 1, 0), epoch(2026, 3, 1, 1), epoch(2026, 3, 1, 2)]


async def test_the_window_edges_are_half_open(client, db, admin, rider):
    await add_ride(db, rider, RideStatus.COMPLETED, T0)  # on since: counted
    await add_ride(db, rider, RideStatus.COMPLETED, T0 + 2 * HOUR)  # on until: not counted

    body = await stats_of(client, admin, T0, T0 + 2 * HOUR, bucket="hour")

    assert body["rides"]["requested"] == 1
    assert sum(entry["rides"] for entry in body["series"]) == 1
    assert len(body["series"]) == 2


# --- 19. validation ---


@pytest.mark.parametrize(
    "params",
    [
        {"until": "2026-03-02T00:00:00Z"},
        {"since": "2026-03-01T00:00:00Z"},
        {"since": "2026-03-01T00:00:00", "until": "2026-03-02T00:00:00Z"},
        {"since": "2026-03-01T00:00:00Z", "until": "2026-03-02T00:00:00"},
        {"since": "2026-03-02T00:00:00Z", "until": "2026-03-01T00:00:00Z"},
        {"since": "2026-03-01T00:00:00Z", "until": "2026-03-01T00:00:00Z"},
        {"since": "2026-03-01T00:00:00Z", "until": "2026-03-02T00:00:00Z", "bucket": "week"},
        {"since": "2026-03-01T00:00:00Z", "until": "2026-03-02T00:00:00Z", "utc_offset_minutes": 841},
        {"since": "2026-03-01T00:00:00Z", "until": "2026-03-02T00:00:00Z", "utc_offset_minutes": -841},
        {"since": "2026-03-01T00:00:00Z", "until": "2026-03-15T00:00:01Z", "bucket": "hour"},
        {"since": "2026-01-01T00:00:00Z", "until": "2027-01-02T00:00:01Z", "bucket": "day"},
    ],
)
async def test_a_bad_stats_request_is_a_422(client, admin, params):
    assert (await client.get("/admin/stats", params=params, headers=admin["headers"])).status_code == 422


async def test_the_exact_window_limits_are_accepted(client, admin):
    assert (await get_stats(client, admin, T0, T0 + timedelta(days=14), bucket="hour")).status_code == 200
    assert (await get_stats(client, admin, T0, T0 + timedelta(days=366), bucket="day")).status_code == 200
    assert (await get_stats(client, admin, T0, T0 + DAY, offset=840)).status_code == 200
    assert (await get_stats(client, admin, T0, T0 + DAY, offset=-840)).status_code == 200


# --- 20. counts and rates ---


@pytest.mark.parametrize(
    "counts, completion, cancellation, no_driver",
    [
        ({RideStatus.COMPLETED: 1, RideStatus.CANCELLED: 1, RideStatus.NO_DRIVER_FOUND: 1}, 33.3, 33.3, 33.3),  # 1/3
        ({RideStatus.COMPLETED: 2, RideStatus.CANCELLED: 1}, 66.7, 33.3, 0.0),  # 2/3 = 66.67
        ({RideStatus.COMPLETED: 1, RideStatus.CANCELLED: 7}, 12.5, 87.5, 0.0),  # 1/8
        ({RideStatus.COMPLETED: 1, RideStatus.CANCELLED: 15}, 6.3, 93.8, 0.0),  # 1/16 = 6.25 and 15/16 = 93.75 round up
        ({RideStatus.COMPLETED: 1, RideStatus.CANCELLED: 1, RideStatus.REQUESTED: 1, RideStatus.IN_PROGRESS: 1}, 50.0, 50.0, 0.0),
    ],
)
async def test_the_rates_are_percent_with_one_decimal_rounded_half_up(
    client, db, admin, make_user, counts, completion, cancellation, no_driver
):
    await add_many(db, make_user, counts, T0 + HOUR)

    body = await stats_of(client, admin, T0, T0 + DAY)

    rides = body["rides"]
    assert (rides["completion_rate"], rides["cancellation_rate"], rides["no_driver_rate"]) == (completion, cancellation, no_driver)
    assert rides["requested"] == sum(counts.values())


async def test_counts_by_status_and_active_rides_and_no_rates_without_finished_rides(client, db, admin, make_user):
    await add_many(db, make_user, {RideStatus.REQUESTED: 1, RideStatus.DRIVER_ASSIGNED: 2, RideStatus.DRIVER_ARRIVED: 1}, T0 + HOUR)
    await add_many(db, make_user, {RideStatus.COMPLETED: 5}, T0 - HOUR)  # before the window

    rides = (await stats_of(client, admin, T0, T0 + DAY))["rides"]

    assert (rides["requested"], rides["completed"], rides["cancelled"], rides["no_driver_found"], rides["active"]) == (4, 0, 0, 0, 4)
    assert (rides["completion_rate"], rides["cancellation_rate"], rides["no_driver_rate"]) == (None, None, None)


async def test_counts_by_status_of_a_mixed_window(client, db, admin, make_user):
    await add_many(
        db, make_user,
        {RideStatus.COMPLETED: 3, RideStatus.CANCELLED: 2, RideStatus.NO_DRIVER_FOUND: 4, RideStatus.IN_PROGRESS: 1}, T0 + HOUR,
    )

    rides = (await stats_of(client, admin, T0, T0 + DAY))["rides"]

    assert (rides["requested"], rides["completed"], rides["cancelled"], rides["no_driver_found"], rides["active"]) == (10, 3, 2, 4, 1)
    assert (rides["completion_rate"], rides["cancellation_rate"], rides["no_driver_rate"]) == (33.3, 22.2, 44.4)  # 3, 2 and 4 of 9


# --- 21. trip averages and assignment times ---


async def test_averages_are_integers_rounded_half_up_over_completed_rides_with_a_fare(client, db, admin, rider):
    fares = [(10000, 1000, 600), (10001, 2001, 601)]
    for final_fare, distance, duration in fares:
        await add_ride(db, rider, RideStatus.COMPLETED, T0 + HOUR, final_fare=final_fare, actual_distance_m=distance, actual_duration_s=duration)
    await add_ride(db, rider, RideStatus.COMPLETED, T0 + HOUR, final_fare=None)  # settled before fares existed: left out
    await add_ride(db, rider, RideStatus.CANCELLED, T0 + HOUR, final_fare=3000)  # not a trip
    await add_ride(db, rider, RideStatus.COMPLETED, T0 + 3 * DAY, final_fare=99999, actual_distance_m=9, actual_duration_s=9)  # outside

    trips = (await stats_of(client, admin, T0, T0 + DAY))["trips"]

    assert (trips["avg_fare"], trips["avg_distance_m"], trips["avg_duration_s"]) == (10001, 1501, 601)  # 10000.5, 1500.5, 600.5


async def test_averages_are_null_when_nothing_was_completed(client, db, admin, rider):
    await add_ride(db, rider, RideStatus.CANCELLED, T0 + HOUR, final_fare=3000)

    trips = (await stats_of(client, admin, T0, T0 + DAY))["trips"]

    assert (trips["avg_fare"], trips["avg_distance_m"], trips["avg_duration_s"]) == (None, None, None)
    assert (trips["assigned_rides"], trips["mean_time_to_assign_s"], trips["median_time_to_assign_s"]) == (0, None, None)


async def add_assigned(db, make_user, waits_seconds: list[float]) -> None:
    for wait in waits_seconds:
        created_at = T0 + HOUR
        ride = await add_ride(db, await make_user("rider"), RideStatus.DRIVER_ASSIGNED, created_at)
        db.add(RideEvent(ride_id=ride.id, from_status=RideStatus.REQUESTED, to_status=RideStatus.DRIVER_ASSIGNED, created_at=created_at + timedelta(seconds=wait)))
    await db.commit()


@pytest.mark.parametrize(
    "waits, mean, median",
    [
        ([2, 4, 10], 5.3, 4.0),  # an odd count: the middle one; the mean is 5.33
        ([1, 2, 4, 9], 4.0, 3.0),  # an even count: halfway between the two in the middle
        ([0.1, 0.2], 0.2, 0.2),  # the mean 0.15 and the median 0.15 round half up
        ([7], 7.0, 7.0),
    ],
)
async def test_time_to_assign_is_mean_and_median_with_one_decimal_half_up(client, db, admin, make_user, waits, mean, median):
    await add_assigned(db, make_user, waits)
    await add_ride(db, await make_user("rider"), RideStatus.REQUESTED, T0 + HOUR)  # never assigned: not counted

    trips = (await stats_of(client, admin, T0, T0 + DAY))["trips"]

    assert trips["assigned_rides"] == len(waits)
    assert (trips["mean_time_to_assign_s"], trips["median_time_to_assign_s"]) == (mean, median)


# --- 22. offers, surge, users ---


async def test_the_acceptance_rate_leaves_out_cancelled_and_pending_offers(client, db, admin, make_user):
    ride = await add_ride(db, await make_user("rider"), RideStatus.REQUESTED, T0 + HOUR)
    statuses = [OfferStatus.ACCEPTED] * 3 + [OfferStatus.REJECTED, OfferStatus.EXPIRED] + [OfferStatus.CANCELLED] * 2 + [OfferStatus.PENDING]
    for index, status in enumerate(statuses):
        who = await make_user("driver")
        created_at = T0 + 2 * HOUR if index != 0 else T0 + 2 * DAY  # the first accepted offer is outside the window
        db.add(RideOffer(ride_id=ride.id, driver_id=who["driver"].id, status=status, pickup_distance_m=10, created_at=created_at, expires_at=created_at + timedelta(seconds=15)))
    await db.commit()

    offers = (await stats_of(client, admin, T0, T0 + DAY))["offers"]

    assert (offers["pending"], offers["accepted"], offers["rejected"], offers["expired"], offers["cancelled"]) == (1, 2, 1, 1, 2)
    assert offers["acceptance_rate"] == 50.0  # 2 / (2 + 1 + 1)


async def test_the_acceptance_rate_is_null_without_answered_offers(client, admin):
    offers = (await stats_of(client, admin, T0, T0 + DAY))["offers"]

    assert offers["acceptance_rate"] is None and offers["accepted"] == 0


async def test_surged_rides_and_the_highest_multiplier(client, db, admin, rider):
    for percent, created_at in ((100, T0 + HOUR), (120, T0 + HOUR), (150, T0 + 2 * HOUR), (200, T0 + 2 * DAY)):
        await add_ride(db, rider, RideStatus.COMPLETED, created_at, surge_percent=percent)

    surge = (await stats_of(client, admin, T0, T0 + DAY))["surge"]
    quiet = (await stats_of(client, admin, T0 + 5 * DAY, T0 + 6 * DAY))["surge"]

    assert surge == {"rides_surged": 2, "max_surge_percent": 150}
    assert quiet == {"rides_surged": 0, "max_surge_percent": 100}


async def test_new_users_are_counted_by_role_and_admins_are_left_out(client, db, admin, make_user):
    inside = [await make_user("rider"), await make_user("rider"), await make_user("driver"), await make_user("admin")]
    outside = [await make_user("rider"), await make_user("driver")]
    for who in inside:
        await db.execute(text("UPDATE users SET created_at = :at WHERE id = :id"), {"at": T0 + HOUR, "id": who["user"].id})
    for who in outside:
        await db.execute(text("UPDATE users SET created_at = :at WHERE id = :id"), {"at": T0 - HOUR, "id": who["user"].id})
    await db.commit()

    users = (await stats_of(client, admin, T0, T0 + DAY))["users"]

    assert users == {"new_riders": 2, "new_drivers": 1}


# --- 23. the money cohort ---


async def test_money_is_by_settlement_time_and_rides_by_request_time(client, db, admin, rider, driver, settled_ride):
    # A: requested before the window, settled inside it. B: requested inside, settled after it. C: both inside.
    a = await settled_ride(rider, driver, amount=10000, created_at=T0 - HOUR)
    b = await settled_ride(rider, driver, amount=20000, created_at=T0 + HOUR)
    c = await settled_ride(rider, driver, amount=30000, created_at=T0 + 2 * HOUR)
    await db.execute(text("UPDATE ride_earnings SET created_at = :at WHERE id = :id"), {"at": T0 + HOUR, "id": a["earning_id"]})
    await db.execute(text("UPDATE ride_earnings SET created_at = :at WHERE id = :id"), {"at": T0 + 2 * DAY, "id": b["earning_id"]})
    await db.commit()

    body = await stats_of(client, admin, T0, T0 + DAY, bucket="hour")

    assert body["rides"]["requested"] == 2  # B and C
    assert body["money"]["total"] == {"rides": 2, "gross": 40000, "platform_fee": 8000, "driver_earning": 32000}  # A and C
    assert sum(entry["rides"] for entry in body["series"]) == 2
    assert sum(entry["gross"] for entry in body["series"]) == 40000 and sum(entry["platform_fee"] for entry in body["series"]) == 8000
    by_hour = {entry["start"]: entry for entry in body["series"]}
    assert by_hour[int((T0 + HOUR).timestamp())]["gross"] == 10000 and by_hour[int((T0 + HOUR).timestamp())]["rides"] == 1  # A's money, B's ride
    assert by_hour[int((T0 + 2 * HOUR).timestamp())]["gross"] == 30000
    assert c["ride_id"] is not None


async def test_money_equals_the_revenue_view_for_the_same_window(client, db, admin, rider, driver, settled_ride):
    await settled_ride(rider, driver, amount=14000, method="cash", created_at=T0 + HOUR)
    await settled_ride(rider, driver, amount=9000, method="wallet", created_at=T0 + 2 * HOUR, percent=25)
    await settled_ride(rider, driver, amount=3000, kind="cancellation", created_at=T0 + 3 * HOUR)

    body = await stats_of(client, admin, T0, T0 + DAY)

    revenue = await earnings_service.get_revenue(db, T0, T0 + DAY)
    assert {name: value for name, value in body["money"].items() if name not in ("since", "until")} == {
        name: value for name, value in revenue.items() if name not in ("since", "until")
    }
    assert body["money"]["total"]["gross"] == 26000 and body["money"]["cancellation_fees"] == 1


async def test_an_empty_window_gives_zeros_nulls_and_a_zero_filled_series(client, admin):
    body = await stats_of(client, admin, T0, T0 + 3 * HOUR, bucket="hour")

    assert body["rides"] == {
        "requested": 0, "completed": 0, "cancelled": 0, "no_driver_found": 0, "active": 0,
        "completion_rate": None, "cancellation_rate": None, "no_driver_rate": None,
    }
    assert body["trips"] == {
        "avg_fare": None, "avg_distance_m": None, "avg_duration_s": None, "assigned_rides": 0,
        "mean_time_to_assign_s": None, "median_time_to_assign_s": None,
    }
    assert body["offers"]["acceptance_rate"] is None and body["surge"] == {"rides_surged": 0, "max_surge_percent": 100}
    assert body["money"]["total"] == {"rides": 0, "gross": 0, "platform_fee": 0, "driver_earning": 0}
    assert body["users"] == {"new_riders": 0, "new_drivers": 0}
    assert len(body["series"]) == 3 and all(
        (entry["rides"], entry["completed"], entry["gross"], entry["platform_fee"]) == (0, 0, 0, 0) for entry in body["series"]
    )
    assert (body["bucket"], body["utc_offset_minutes"]) == ("hour", 0)


# --- 24. what is true right now ---


async def test_now_counts_presence_keys_active_rides_and_pending_offers(client, db, admin, make_user, put_online):
    for _ in range(2):
        await put_online(await make_user("driver"), LAT, LNG)
    await make_user("driver")  # offline
    requested = await add_ride(db, await make_user("rider"), RideStatus.REQUESTED, T0)
    await add_ride(db, await make_user("rider"), RideStatus.IN_PROGRESS, T0, await make_user("driver"))
    await add_ride(db, await make_user("rider"), RideStatus.COMPLETED, T0)
    overdue = await make_user("driver")
    # Past its deadline, but the sweeper has not handled it yet: still PENDING.
    db.add(RideOffer(ride_id=requested.id, driver_id=overdue["driver"].id, pickup_distance_m=10, expires_at=T0 + timedelta(seconds=15)))
    await db.commit()

    now = (await stats_of(client, admin, T0, T0 + DAY))["now"]

    assert now == {
        "online_drivers": 2, "active_rides": {"REQUESTED": 1, "DRIVER_ASSIGNED": 0, "DRIVER_ARRIVED": 0, "IN_PROGRESS": 1}, "pending_offers": 1,
    }


async def test_with_redis_down_the_stats_still_answer_with_one_missing_number_and_one_warning(client, admin, monkeypatch, logged):
    async def broken():
        raise RedisError("down")

    monkeypatch.setattr(drivers_repo, "get_online_positions", broken)

    response = await get_stats(client, admin, T0, T0 + DAY)

    assert response.status_code == 200
    assert response.json()["now"]["online_drivers"] is None
    warnings = [record for record in logged.records if "online drivers" in record.getMessage()]
    assert len(warnings) == 1 and warnings[0].levelname == "WARNING"
