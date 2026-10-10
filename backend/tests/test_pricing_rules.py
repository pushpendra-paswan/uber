import asyncio

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError

from app.models import PricingRule, PricingRuleChange, RideStatus
from app.repositories import pricing as pricing_repo
from app.schemas import RULE_LIMITS
from app.utils.geo import geohash_encode
from test_fares import cancel, complete, go, set_times, trip  # noqa: F401  (a fixture: trip)
from test_rides import RIDE_BODY

URL = "/admin/pricing-rules"
STALE = "This rule was changed by someone else. Reload and try again."

# The bounds written out by hand, not read from RULE_LIMITS, so a wrong change to the table fails the tests.
BOUNDS = {
    "base_fare": (0, 100000),
    "per_km": (0, 50000),
    "per_min": (0, 20000),
    "min_fare": (100, 500000),
    "cancellation_fee": (0, 100000),
    "free_cancel_seconds": (0, 3600),
    "commission_percent": (0, 100),
    "surge_cap": (1.0, 2.0),
}
SEED = {
    "base_fare": 5000, "per_km": 1200, "per_min": 200, "min_fare": 8000, "cancellation_fee": 3000, "free_cancel_seconds": 120,
    "commission_percent": 20, "surge_cap": 2.0,
}
RULE_KEYS = {
    "id", "vehicle_type", "version", "updated_at", "updated_by", "updated_by_name", *SEED,
}
PICKUP_ZONE = geohash_encode(RIDE_BODY["pickup_lat"], RIDE_BODY["pickup_lng"], 5)


async def patch(client, who: dict, body: dict, vehicle_type: str = "economy"):
    return await client.patch(f"{URL}/{vehicle_type}", json=body, headers=who["headers"])


async def stored(db) -> dict:
    """The rule row, columns only (never an entity: the session's identity map could show an older copy)."""
    result = await db.execute(
        select(*PricingRule.__table__.c).where(PricingRule.vehicle_type == "economy")
    )
    return dict(result.mappings().one())


async def audit(db) -> list:
    result = await db.execute(select(PricingRuleChange).order_by(PricingRuleChange.id))
    return [
        {"actor": row.actor_user_id, "before": row.version_before, "after": row.version_after, "changes": row.changes, "rule_id": row.rule_id}
        for row in result.scalars().all()
    ]


async def estimate_fare(client, rider: dict) -> dict:
    body = {key: value for key, value in RIDE_BODY.items() if not key.endswith("address")}
    response = await client.post("/rides/estimate", json=body, headers=rider["headers"])
    assert response.status_code == 200, response.text
    return response.json()


# --- 1. reading ---


async def test_the_seeded_rule_is_listed_with_its_limits(client, db, admin):
    response = await client.get(URL, headers=admin["headers"])

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"rules", "active_rides", "limits"}
    (rule,) = body["rules"]
    assert set(rule) == RULE_KEYS
    assert {name: rule[name] for name in SEED} == SEED
    assert (rule["vehicle_type"], rule["version"], rule["updated_by"], rule["updated_by_name"]) == ("economy", 1, None, None)
    assert body["limits"] == RULE_LIMITS
    assert body["limits"] == {name: {"min": low, "max": high} for name, (low, high) in BOUNDS.items()}
    assert body["active_rides"] == 0


async def test_active_rides_counts_exactly_the_four_active_statuses(client, make_user, insert_ride, admin):
    for status in (RideStatus.COMPLETED, RideStatus.CANCELLED, RideStatus.NO_DRIVER_FOUND):
        await insert_ride(await make_user("rider"), status)
    assert (await client.get(URL, headers=admin["headers"])).json()["active_rides"] == 0

    counted = 0
    for status in (RideStatus.REQUESTED, RideStatus.DRIVER_ASSIGNED, RideStatus.DRIVER_ARRIVED, RideStatus.IN_PROGRESS):
        rider = await make_user("rider")
        # Each assigned ride gets its own driver: a driver can have one active ride only.
        await insert_ride(rider, status, None if status == RideStatus.REQUESTED else await make_user("driver"))
        counted += 1
        assert (await client.get(URL, headers=admin["headers"])).json()["active_rides"] == counted, status


# --- 2. the happy path ---


async def test_an_edit_changes_the_values_bumps_the_version_and_writes_one_audit_row(client, db, admin):
    before = await stored(db)

    response = await patch(client, admin, {"version": 1, "per_km": 1500, "commission_percent": 25})

    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == {"rule", "active_rides", "changes"}
    assert body["changes"] == [{"field": "per_km", "old": 1200, "new": 1500}, {"field": "commission_percent", "old": 20, "new": 25}]
    rule = body["rule"]
    assert (rule["version"], rule["per_km"], rule["commission_percent"]) == (2, 1500, 25)
    assert (rule["updated_by"], rule["updated_by_name"]) == (admin["user"].id, admin["user"].name)
    assert body["active_rides"] == 0

    after = await stored(db)
    assert after["updated_at"] > before["updated_at"]
    assert {name: after[name] for name in SEED if name not in ("per_km", "commission_percent")} == {
        name: SEED[name] for name in SEED if name not in ("per_km", "commission_percent")
    }
    assert after["version"] == 2 and after["updated_by"] == admin["user"].id

    assert await audit(db) == [
        {"actor": admin["user"].id, "before": 1, "after": 2, "rule_id": after["id"], "changes": body["changes"]}
    ]


# --- 3. versions ---


async def test_a_stale_version_is_a_409_and_changes_nothing(client, db, admin):
    assert (await patch(client, admin, {"version": 1, "per_km": 1500})).status_code == 200

    response = await patch(client, admin, {"version": 1, "per_km": 1600})

    assert response.status_code == 409
    assert response.json()["detail"] == STALE
    assert (await stored(db))["per_km"] == 1500
    assert len(await audit(db)) == 1


async def test_a_replay_of_an_applied_request_fails_and_never_applies_twice(client, db, admin):
    body = {"version": 1, "base_fare": 6000}
    assert (await patch(client, admin, body)).status_code == 200

    replay = await patch(client, admin, body)

    assert replay.status_code == 409
    assert (await stored(db))["version"] == 2
    assert len(await audit(db)) == 1


async def test_a_future_version_is_a_409_too(client, db, admin):
    response = await patch(client, admin, {"version": 7, "per_km": 1500})

    assert response.status_code == 409
    assert (await stored(db))["version"] == 1


@pytest.mark.parametrize("version", ["1", 1.0, 1.5, True, None])
async def test_a_version_that_is_not_an_integer_is_a_422(client, db, admin, version):
    response = await patch(client, admin, {"version": version, "per_km": 1500})

    assert response.status_code == 422
    assert (await stored(db))["per_km"] == 1200


async def test_a_missing_version_is_a_422(client, db, admin):
    assert (await patch(client, admin, {"per_km": 1500})).status_code == 422
    assert (await stored(db))["per_km"] == 1200


# --- 4. no-ops ---


async def test_the_same_values_change_nothing(client, db, admin):
    before = await stored(db)

    response = await patch(client, admin, {"version": 1, "per_km": 1200, "commission_percent": 20})

    assert response.status_code == 200
    assert response.json()["changes"] == []
    assert response.json()["rule"]["version"] == 1
    assert await stored(db) == before
    assert await audit(db) == []


async def test_a_mixed_patch_lists_only_the_field_that_differs(client, db, admin):
    response = await patch(client, admin, {"version": 1, "per_km": 1200, "per_min": 250})

    assert response.json()["changes"] == [{"field": "per_min", "old": 200, "new": 250}]
    assert response.json()["rule"]["version"] == 2
    assert [entry["changes"] for entry in await audit(db)] == [[{"field": "per_min", "old": 200, "new": 250}]]


@pytest.mark.parametrize("cap", [2, 2.0, 2.00])
async def test_a_surge_cap_equal_to_the_stored_one_is_a_no_op_however_it_is_written(client, db, admin, cap):
    response = await patch(client, admin, {"version": 1, "surge_cap": cap})

    assert response.status_code == 200
    assert response.json()["changes"] == []
    assert (await stored(db))["version"] == 1


async def test_a_surge_cap_change_is_recorded_as_a_float(client, db, admin):
    response = await patch(client, admin, {"version": 1, "surge_cap": 1.35})

    assert response.json()["changes"] == [{"field": "surge_cap", "old": 2.0, "new": 1.35}]
    assert (await stored(db))["surge_cap"] == 1.35


# --- 5. validation ---


def accepted_cases():
    return [(name, value) for name, (low, high) in BOUNDS.items() for value in (low, high)]


def refused_cases():
    cases = []
    for name, (low, high) in BOUNDS.items():
        beyond = (0.99, 2.01) if name == "surge_cap" else (low - 1, high + 1)
        cases += [(name, value) for value in beyond]
    return cases


@pytest.mark.parametrize("name, value", accepted_cases())
async def test_a_value_at_its_minimum_or_maximum_is_accepted(client, db, admin, name, value):
    response = await patch(client, admin, {"version": 1, name: value})

    assert response.status_code == 200, response.text
    assert (await stored(db))[name] == value


@pytest.mark.parametrize("name, value", refused_cases())
async def test_a_value_beyond_its_bounds_is_a_422_and_changes_nothing(client, db, admin, name, value):
    response = await patch(client, admin, {"version": 1, name: value})

    assert response.status_code == 422
    assert (await stored(db))["version"] == 1
    assert await audit(db) == []


@pytest.mark.parametrize("value", [1.555, "1.5", True, None, "abc", [1.5]])
async def test_a_surge_cap_must_be_a_number_with_at_most_two_decimals(client, db, admin, value):
    response = await patch(client, admin, {"version": 1, "surge_cap": value})

    assert response.status_code == 422
    assert (await stored(db))["surge_cap"] == 2.0


@pytest.mark.parametrize("name", [name for name in BOUNDS if name != "surge_cap"])
@pytest.mark.parametrize("value", [12.5, 5.0, "5", True, None])
async def test_a_money_or_count_value_must_be_a_strict_integer(client, db, admin, name, value):
    response = await patch(client, admin, {"version": 1, name: value})

    assert response.status_code == 422
    assert (await stored(db))[name] == SEED[name]


@pytest.mark.parametrize("extra", [{"vehicle_type": "x"}, {"id": 5}, {"version_before": 1}, {"updated_by": 1}, {"unknown": 1}])
async def test_an_unknown_field_is_a_422(client, db, admin, extra):
    response = await patch(client, admin, {"version": 1, "per_km": 1500, **extra})

    assert response.status_code == 422
    assert (await stored(db))["per_km"] == 1200


async def test_a_body_with_only_a_version_is_a_422(client, db, admin):
    assert (await patch(client, admin, {"version": 1})).status_code == 422
    assert (await stored(db))["version"] == 1


# --- 6. unknown type and who may call ---


async def test_an_unknown_vehicle_type_is_a_404(client, admin):
    assert (await patch(client, admin, {"version": 1, "per_km": 1500}, "limousine")).status_code == 404
    assert (await client.get(f"{URL}/limousine/changes", headers=admin["headers"])).status_code == 404


@pytest.mark.parametrize(
    "method, path, body",
    [("get", URL, None), ("patch", f"{URL}/economy", {"version": 1, "per_km": 1500}), ("get", f"{URL}/economy/changes", None)],
)
async def test_only_an_admin_may_use_the_pricing_routes(client, db, rider, driver, method, path, body):
    assert (await client.request(method, path, json=body)).status_code == 401
    for who in (rider, driver):
        assert (await client.request(method, path, json=body, headers=who["headers"])).status_code == 403
    assert (await stored(db))["version"] == 1


# --- 7. concurrency ---


@pytest.mark.parametrize("attempt", range(5))
async def test_twenty_simultaneous_edits_with_one_version_apply_exactly_one(client, db, admin, attempt):
    responses = await asyncio.gather(*[patch(client, admin, {"version": 1, "per_km": 1000 + index}) for index in range(20)])

    codes = sorted(response.status_code for response in responses)
    assert codes == [200] + [409] * 19
    winner = next(response for response in responses if response.status_code == 200).json()
    after = await stored(db)
    assert after["version"] == 2
    assert after["per_km"] == winner["rule"]["per_km"]
    assert len(await audit(db)) == 1
    assert all(response.json()["detail"] == STALE for response in responses if response.status_code == 409)


async def test_five_edits_in_a_row_each_use_the_version_they_were_given(client, db, admin):
    version = 1
    for step in range(5):
        response = await patch(client, admin, {"version": version, "per_km": 1300 + step})
        assert response.status_code == 200, response.text
        assert response.json()["rule"]["version"] == version + 1
        version = response.json()["rule"]["version"]

    assert version == 6
    rows = await audit(db)
    assert [(row["before"], row["after"]) for row in rows] == [(1, 2), (2, 3), (3, 4), (4, 5), (5, 6)]
    assert [row["changes"][0]["new"] for row in rows] == [1300, 1301, 1302, 1303, 1304]


# --- 8. atomic ---


@pytest.mark.parametrize("failing", ["insert_change", "update_rule"])
async def test_a_failure_halfway_leaves_neither_the_rule_nor_an_audit_row(client, db, admin, monkeypatch, failing):
    async def broken(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(pricing_repo, failing, broken)
    before = await stored(db)

    assert (await patch(client, admin, {"version": 1, "per_km": 1500, "commission_percent": 30})).status_code == 500

    assert await stored(db) == before
    assert await audit(db) == []


# --- 9. the database checks ---


@pytest.mark.parametrize(
    "assignment, constraint",
    [
        ("min_fare = 99", "ck_pricing_rules_min_fare_floor"),
        ("surge_cap = 0.99", "ck_pricing_rules_surge_cap_range"),
        ("surge_cap = 2.01", "ck_pricing_rules_surge_cap_range"),
        ("base_fare = -1", "ck_pricing_rules_base_fare_nonneg"),
        ("per_km = -1", "ck_pricing_rules_per_km_nonneg"),
        ("per_min = -1", "ck_pricing_rules_per_min_nonneg"),
        ("cancellation_fee = -1", "ck_pricing_rules_cancellation_fee_nonneg"),
        ("free_cancel_seconds = -1", "ck_pricing_rules_free_cancel_seconds_nonneg"),
        ("commission_percent = 101", "ck_pricing_rules_commission_percent_range"),
        ("commission_percent = -1", "ck_pricing_rules_commission_percent_range"),
    ],
)
async def test_the_database_refuses_a_rule_outside_its_checks(db, assignment, constraint):
    with pytest.raises(IntegrityError) as caught:
        async with db.begin_nested():
            await db.execute(text(f"UPDATE pricing_rules SET {assignment}"))

    assert constraint in str(caught.value)


async def test_the_boundary_values_of_the_checks_are_allowed(db):
    async with db.begin_nested():
        await db.execute(
            text("UPDATE pricing_rules SET min_fare = 100, surge_cap = 1.0, base_fare = 0, per_km = 0, per_min = 0, "
                 "cancellation_fee = 0, free_cancel_seconds = 0, commission_percent = 0")
        )
    async with db.begin_nested():
        await db.execute(text("UPDATE pricing_rules SET surge_cap = 2.0, commission_percent = 100"))
    await db.rollback()


async def test_the_audit_table_refuses_a_skipped_version_and_a_duplicate_version(db, admin):
    rule_id = (await stored(db))["id"]
    insert = "INSERT INTO pricing_rule_changes (rule_id, actor_user_id, version_before, version_after, changes) VALUES (:r, :a, :b, :f, '[]')"
    arguments = {"r": rule_id, "a": admin["user"].id}

    with pytest.raises(IntegrityError) as skipped:
        async with db.begin_nested():
            await db.execute(text(insert), {**arguments, "b": 1, "f": 3})
    assert "ck_pricing_rule_changes_version_step" in str(skipped.value)

    async with db.begin_nested():
        await db.execute(text(insert), {**arguments, "b": 1, "f": 2})
    with pytest.raises(IntegrityError) as duplicate:
        async with db.begin_nested():
            await db.execute(text(insert), {**arguments, "b": 1, "f": 2})
    assert "uq_pricing_rule_changes_rule_id_version_after" in str(duplicate.value)
    await db.rollback()


# --- 10. what an edit changes in the running system ---


async def test_a_new_estimate_uses_the_new_rate_at_once(client, admin, rider):
    assert (await estimate_fare(client, rider))["fare_estimate"] == 14000  # 5000 + 5 km * 1200 + 15 min * 200

    assert (await patch(client, admin, {"version": 1, "per_km": 2000})).status_code == 200

    estimate = await estimate_fare(client, rider)
    assert estimate["distance_fare"] == 10000
    assert estimate["fare_estimate"] == 18000  # 5000 + 10000 + 3000


async def test_a_ride_in_progress_is_settled_with_the_new_rates_but_within_its_own_cap(client, db, admin, trip):
    await go(client, trip, "arrive", "start")
    assert (await patch(client, admin, {"version": 1, "per_km": 1600, "per_min": 300})).status_code == 200
    await set_times(db, trip["id"], 900)

    response = await complete(client, trip)

    assert response.status_code == 200, response.text
    ride = response.json()
    # No tracking: the estimated 5000 m is billed. 5000 + 5 * 1600 + 15 * 300 = 5000 + 8000 + 4500 = 17500; the cap is 21000.
    assert ride["fare_breakdown"]["distance_fare"] == 8000 and ride["fare_breakdown"]["time_fare"] == 4500
    assert (ride["final_fare"], ride["fare_breakdown"]["capped"], ride["fare_estimate"]) == (17500, False, 14000)


async def test_the_cap_of_the_rides_own_estimate_still_holds_after_an_edit(client, db, admin, trip):
    await go(client, trip, "arrive", "start")
    assert (await patch(client, admin, {"version": 1, "per_km": 5000})).status_code == 200
    await set_times(db, trip["id"], 900)

    ride = (await complete(client, trip)).json()

    # 5000 + 25000 + 3000 = 33000 computed, but 150 percent of the 14000 estimate is 21000.
    assert (ride["fare_breakdown"]["computed_fare"], ride["fare_breakdown"]["fare_cap"], ride["final_fare"]) == (33000, 21000, 21000)
    assert ride["fare_breakdown"]["capped"] is True


async def test_the_surge_cap_applies_at_once_although_the_snapshot_says_more(client, admin, rider):
    await pricing_repo.save_snapshot(
        {"computed_at": 1, "zones": {PICKUP_ZONE: {"demand": 4, "supply": 0, "pressure_percent": 400, "surge_percent": 150}}}, 60
    )
    assert (await estimate_fare(client, rider))["surge_percent"] == 150

    assert (await patch(client, admin, {"version": 1, "surge_cap": 1.0})).status_code == 200

    estimate = await estimate_fare(client, rider)
    assert (estimate["surge_percent"], estimate["fare_estimate"]) == (100, 14000)
    assert (await pricing_repo.get_snapshot())["zones"][PICKUP_ZONE]["surge_percent"] == 150  # the snapshot is still uncapped


async def test_a_changed_commission_applies_to_later_settlements_only(client, db, admin, trip, make_user, assign_ride):
    await go(client, trip, "arrive", "start", "complete")
    assert (await patch(client, admin, {"version": 1, "commission_percent": 30})).status_code == 200
    await client.post("/drivers/me/offline", headers=trip["driver"]["headers"])

    second_rider, second_driver = await make_user("rider"), await make_user("driver")
    second = {"rider": second_rider, "driver": second_driver, "id": (await assign_ride(second_rider, second_driver))["id"]}
    await go(client, second, "arrive", "start", "complete")

    rows = (await db.execute(text("SELECT ride_id, commission_percent, platform_fee, driver_earning, gross_amount FROM ride_earnings ORDER BY id"))).all()
    assert [row.ride_id for row in rows] == [trip["id"], second["id"]]
    assert [row.commission_percent for row in rows] == [20, 30]
    for row in rows:
        assert row.platform_fee == (row.gross_amount * row.commission_percent + 50) // 100
        assert row.platform_fee + row.driver_earning == row.gross_amount


async def test_a_changed_cancellation_fee_and_window_apply_to_the_next_cancellation(client, admin, trip):
    quote = await client.get(f"/rides/{trip['id']}/cancellation-fee", headers=trip["rider"]["headers"])
    assert quote.json() == {"fee": 0, "reason": "within_free_window"}  # just assigned, window 120 s

    assert (await patch(client, admin, {"version": 1, "free_cancel_seconds": 0, "cancellation_fee": 4500})).status_code == 200

    quote = await client.get(f"/rides/{trip['id']}/cancellation-fee", headers=trip["rider"]["headers"])
    assert quote.json() == {"fee": 4500, "reason": "late_cancellation"}
    cancelled = await cancel(client, trip)
    assert cancelled.status_code == 200
    assert (cancelled.json()["final_fare"], cancelled.json()["fare_breakdown"]["fee"]) == (4500, 4500)


# --- 11. history ---


async def test_the_history_is_newest_first_with_the_actor_and_the_changes(client, admin):
    for version, per_km in ((1, 1300), (2, 1400), (3, 1500)):
        assert (await patch(client, admin, {"version": version, "per_km": per_km})).status_code == 200

    response = await client.get(f"{URL}/economy/changes", headers=admin["headers"])

    assert response.status_code == 200
    entries = response.json()
    assert [(entry["version_before"], entry["version_after"]) for entry in entries] == [(3, 4), (2, 3), (1, 2)]
    assert set(entries[0]) == {"id", "actor_name", "version_before", "version_after", "changes", "created_at"}
    assert entries[0]["actor_name"] == admin["user"].name
    assert entries[0]["changes"] == [{"field": "per_km", "old": 1400, "new": 1500}]


async def test_the_history_limit_defaults_to_ten_and_is_bounded(client, db, admin):
    for version in range(1, 13):
        assert (await patch(client, admin, {"version": version, "per_km": 1200 + version})).status_code == 200

    assert len((await client.get(f"{URL}/economy/changes", headers=admin["headers"])).json()) == 10
    assert len((await client.get(f"{URL}/economy/changes", params={"limit": 12}, headers=admin["headers"])).json()) == 12
    for limit in (0, 101, -1):
        assert (await client.get(f"{URL}/economy/changes", params={"limit": limit}, headers=admin["headers"])).status_code == 422
    assert await db.scalar(select(func.count()).select_from(PricingRuleChange)) == 12
