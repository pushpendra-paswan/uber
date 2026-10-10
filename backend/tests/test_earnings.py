import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from app.models import Payment, Ride, RideEarning, RideEvent, RideStatus, User, UserRole, Vehicle, WalletEntry, WalletEntryKind
from app.repositories import earnings as earnings_repo
from app.repositories import pricing as pricing_repo
from app.security import create_access_token
from app.services import wallet as wallet_service
from test_fares import age_assignment, cancel, complete, go, new_requested, set_times, trip, walk  # noqa: F401  (fixtures: new_requested, trip)
from test_payments import cash_trip, count, entries_of, fund, payments_of, start_ride, wallet_trip  # noqa: F401  (fixtures)
from test_rides import RIDE_BODY

ZERO = {"rides": 0, "gross": 0, "platform_fee": 0, "driver_earning": 0}
ZERO_SUMMARY = {
    "since": None, "until": None, "trips": 0, "cancellation_fees": 0, "total": ZERO, "cash": ZERO, "wallet": ZERO,
    "settlement": {"owed_to_driver": 0, "owed_by_driver": 0, "net": 0},
}
ENTRY_KEYS = {
    "id", "ride_id", "kind", "payment_method", "gross_amount", "commission_percent", "platform_fee", "driver_earning",
    "pickup_address", "dropoff_address", "distance_m", "duration_s", "created_at",
}
RECEIPT_KEYS = {
    "receipt_number", "issued_at", "ride_id", "kind", "status", "pickup_address", "dropoff_address", "started_at", "ended_at",
    "driver_name", "vehicle", "estimated_fare", "trip", "cancellation", "payment",
}
TRIP_KEYS = {
    "distance_m", "duration_s", "distance_source", "base_fare", "distance_fare", "time_fare", "minimum_fare_applied", "normal_fare",
    "surge_percent", "surge_amount", "computed_fare", "capped", "total",
}
NO_RECEIPT = "There is no receipt for this ride"
T0 = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)


# --- helpers ---


async def earning_rows(db, *where):
    result = await db.execute(
        select(
            RideEarning.id, RideEarning.ride_id, RideEarning.payment_id, RideEarning.driver_id, RideEarning.kind,
            RideEarning.gross_amount, RideEarning.commission_percent, RideEarning.platform_fee, RideEarning.driver_earning,
        ).where(*where).order_by(RideEarning.id)
    )
    return result.all()


async def get_summary(client, who: dict, path: str = "/drivers/me/earnings", **params):
    return await client.get(path, params=params, headers=who["headers"])


async def get_receipt(client, who: dict, ride_id: int):
    return await client.get(f"/rides/{ride_id}/receipt", headers=who["headers"])


def split(gross: int, percent: int = 20) -> tuple[int, int]:
    """The rule written out by hand: the platform's part rounded half up, the driver gets the rest."""
    fee = (gross * percent + 50) // 100
    return fee, gross - fee


async def check_trip_earning(db, trip: dict, ride: dict) -> None:
    fare = ride["final_fare"]
    (row,) = await earning_rows(db, RideEarning.ride_id == trip["id"])
    payment = (await db.execute(select(Payment.id, Payment.amount).where(Payment.ride_id == trip["id"]))).one()
    assert row.kind == "trip" and row.gross_amount == fare == payment.amount and row.payment_id == payment.id
    assert row.driver_id == trip["driver"]["driver"].id and row.commission_percent == 20
    assert (row.platform_fee, row.driver_earning) == split(fare)
    assert row.platform_fee + row.driver_earning == fare


async def set_rule(db, **values) -> None:
    sets = ", ".join(f"{name} = :{name}" for name in values)
    await db.execute(text(f"UPDATE pricing_rules SET {sets}"), values)
    await db.commit()


# --- A. the split ---


@pytest.mark.parametrize(
    "gross, percent, fee, earning",
    [(14000, 20, 2800, 11200), (13999, 20, 2800, 11199), (1, 20, 0, 1), (5, 50, 3, 2), (8000, 20, 1600, 6400), (3000, 0, 0, 3000), (3000, 100, 3000, 0)],
)
async def test_the_platform_fee_is_rounded_half_up_and_the_driver_gets_the_rest(client, db, trip, gross, percent, fee, earning):
    # The fee of a late cancellation is exactly what the rule says, so the gross of the split is known.
    await set_rule(db, cancellation_fee=gross, free_cancel_seconds=0, commission_percent=percent)
    await age_assignment(db, trip["id"], 30)

    response = await cancel(client, trip)

    assert response.status_code == 200 and response.json()["final_fare"] == gross
    assert [payment.amount for payment in await payments_of(db, trip["id"])] == [gross]
    payment_id = await db.scalar(select(Payment.id).where(Payment.ride_id == trip["id"]))
    (row,) = await earning_rows(db)
    assert (row.kind, row.driver_id, row.ride_id, row.payment_id) == ("cancellation", trip["driver"]["driver"].id, trip["id"], payment_id)
    assert (row.commission_percent, row.gross_amount, row.platform_fee, row.driver_earning) == (percent, gross, fee, earning)


async def test_a_completed_wallet_ride_has_a_trip_earning(client, db, wallet_trip):
    await go(client, wallet_trip, "arrive", "start")
    ride = (await complete(client, wallet_trip)).json()
    assert ride["payment_method"] == "wallet"
    await check_trip_earning(db, wallet_trip, ride)


async def test_a_completed_cash_ride_has_a_trip_earning(client, db, cash_trip):
    await go(client, cash_trip, "arrive", "start")
    ride = (await complete(client, cash_trip)).json()
    assert ride["payment_method"] == "cash"
    await check_trip_earning(db, cash_trip, ride)


async def test_a_free_cancellation_creates_no_earning(client, db, trip):
    response = await cancel(client, trip)
    assert response.json()["final_fare"] == 0
    assert await count(db, Payment) == 0 and await count(db, RideEarning) == 0


async def test_a_driver_cancellation_creates_no_earning(client, db, trip):
    await age_assignment(db, trip["id"], 500)
    response = await cancel(client, trip, "driver")
    assert response.json()["final_fare"] == 0
    assert await count(db, Payment) == 0 and await count(db, RideEarning) == 0


async def test_no_driver_found_creates_no_earning(client, db, rider):
    created = await client.post("/rides", json=RIDE_BODY, headers=rider["headers"])
    assert created.json()["status"] == "NO_DRIVER_FOUND"
    assert await count(db, Payment) == 0 and await count(db, RideEarning) == 0


async def test_a_cancellation_fee_of_zero_creates_no_earning(client, db, trip):
    await set_rule(db, cancellation_fee=0, free_cancel_seconds=0)
    await age_assignment(db, trip["id"], 30)
    response = await cancel(client, trip)
    assert response.status_code == 200 and response.json()["final_fare"] == 0
    assert await count(db, Payment) == 0 and await count(db, RideEarning) == 0


async def test_the_commission_is_a_snapshot_on_each_row(client, db, admin, rider, driver, put_online, accept_offer, trip):
    await age_assignment(db, trip["id"], 200)
    assert (await cancel(client, trip)).status_code == 200  # 3000 at 20 percent
    await set_rule(db, commission_percent=30)
    second = await start_ride(client, put_online, accept_offer, rider, driver, "cash")
    await age_assignment(db, second["id"], 200)
    assert (await cancel(client, second)).status_code == 200  # 3000 at 30 percent

    first_row, second_row = await earning_rows(db)
    assert (first_row.commission_percent, first_row.platform_fee, first_row.driver_earning) == (20, 600, 2400)
    assert (second_row.commission_percent, second_row.platform_fee, second_row.driver_earning) == (30, 900, 2100)
    revenue = (await client.get("/admin/revenue", headers=admin["headers"])).json()
    assert revenue["total"] == {"rides": 2, "gross": 6000, "platform_fee": 1500, "driver_earning": 4500}
    mine = (await get_summary(client, driver)).json()
    assert mine["total"] == revenue["total"]
    await set_rule(db, commission_percent=50)  # a later edit changes nothing that was already settled
    assert (await client.get("/admin/revenue", headers=admin["headers"])).json()["total"] == revenue["total"]


@pytest.mark.parametrize("module, name", [(earnings_repo, "create"), (wallet_service, "post_entry")])
async def test_a_failing_earning_or_ledger_write_undoes_the_whole_completion(client, db, monkeypatch, wallet_trip, module, name):
    await go(client, wallet_trip, "arrive", "start")
    real = getattr(module, name)

    async def broken(*args, **kwargs):
        raise RuntimeError("broken on purpose")

    monkeypatch.setattr(module, name, broken)
    assert (await complete(client, wallet_trip)).status_code == 500

    stored = (await db.execute(select(Ride.status, Ride.final_fare, Ride.fare_breakdown).where(Ride.id == wallet_trip["id"]))).one()
    assert (stored.status, stored.final_fare, stored.fare_breakdown) == (RideStatus.IN_PROGRESS, None, None)
    assert await payments_of(db, wallet_trip["id"]) == []
    assert await count(db, RideEarning) == 0
    assert await count(db, WalletEntry, WalletEntry.kind == WalletEntryKind.RIDE_CHARGE) == 0
    assert await count(db, RideEvent, RideEvent.ride_id == wallet_trip["id"], RideEvent.to_status == RideStatus.COMPLETED) == 0

    monkeypatch.setattr(module, name, real)
    assert (await complete(client, wallet_trip)).status_code == 200
    assert len(await payments_of(db, wallet_trip["id"])) == 1 and await count(db, RideEarning) == 1
    assert await count(db, WalletEntry, WalletEntry.kind == WalletEntryKind.RIDE_CHARGE) == 1


async def test_a_failing_earning_write_undoes_a_cancellation_too(client, db, monkeypatch, wallet_trip):
    await age_assignment(db, wallet_trip["id"], 200)
    real = earnings_repo.create

    async def broken(*args, **kwargs):
        raise RuntimeError("broken on purpose")

    monkeypatch.setattr(earnings_repo, "create", broken)
    assert (await cancel(client, wallet_trip)).status_code == 500
    stored = (await db.execute(select(Ride.status, Ride.final_fare).where(Ride.id == wallet_trip["id"]))).one()
    assert (stored.status, stored.final_fare) == (RideStatus.DRIVER_ASSIGNED, None)
    assert await payments_of(db, wallet_trip["id"]) == [] and await count(db, RideEarning) == 0
    assert await count(db, WalletEntry, WalletEntry.kind == WalletEntryKind.RIDE_CHARGE) == 0

    monkeypatch.setattr(earnings_repo, "create", real)
    assert (await cancel(client, wallet_trip)).status_code == 200
    assert len(await payments_of(db, wallet_trip["id"])) == 1 and await count(db, RideEarning) == 1


async def test_two_simultaneous_completions_settle_once(client, db, cash_trip):
    await go(client, cash_trip, "arrive", "start")
    answers = await asyncio.gather(complete(client, cash_trip), complete(client, cash_trip))
    assert sorted(answer.status_code for answer in answers) == [200, 409]
    assert await count(db, RideEarning) == 1 and len(await payments_of(db, cash_trip["id"])) == 1


async def insert_earning(db, ride_id: int, payment_id: int, driver_id: int, **overrides) -> None:
    values = {"kind": "trip", "gross_amount": 1000, "commission_percent": 20, "platform_fee": 200, "driver_earning": 800, **overrides}
    await db.execute(
        text(
            "INSERT INTO ride_earnings (ride_id, payment_id, driver_id, kind, gross_amount, commission_percent, platform_fee, driver_earning) "
            "VALUES (:ride_id, :payment_id, :driver_id, :kind, :gross_amount, :commission_percent, :platform_fee, :driver_earning)"
        ),
        {"ride_id": ride_id, "payment_id": payment_id, "driver_id": driver_id, **values},
    )


@pytest.mark.parametrize(
    "constraint, overrides",
    [
        ("ck_ride_earnings_split_adds_up", {"platform_fee": 100, "driver_earning": 100}),
        ("ck_ride_earnings_platform_fee_not_negative", {"platform_fee": -100, "driver_earning": 1100}),
        ("ck_ride_earnings_driver_earning_not_negative", {"platform_fee": 1100, "driver_earning": -100}),
        ("ck_ride_earnings_gross_positive", {"gross_amount": 0, "platform_fee": 0, "driver_earning": 0}),
        ("ck_ride_earnings_commission_percent_range", {"commission_percent": 101}),
        ("ck_ride_earnings_commission_percent_range", {"commission_percent": -1}),
        ("ck_ride_earnings_kind_known", {"kind": "tip"}),
    ],
)
async def test_the_database_refuses_a_bad_earning_row(db, rider, driver, settled_ride, constraint, overrides):
    free = await settled_ride(rider, driver, with_earning=False)
    with pytest.raises(IntegrityError) as error:
        async with db.begin_nested():
            await insert_earning(db, free["ride_id"], free["payment_id"], driver["driver"].id, **overrides)
    assert constraint in str(error.value)
    assert await count(db, RideEarning) == 0


async def test_the_database_refuses_a_second_earning_for_a_ride_or_a_payment_and_allows_the_edges(db, rider, driver, settled_ride):
    base = await settled_ride(rider, driver)
    free = await settled_ride(rider, driver, with_earning=False)
    driver_id = driver["driver"].id

    with pytest.raises(IntegrityError) as error:
        async with db.begin_nested():
            await insert_earning(db, free["ride_id"], base["payment_id"], driver_id)  # the ride is free, the payment is not
    assert "uq_ride_earnings_payment_id" in str(error.value)
    with pytest.raises(IntegrityError) as error:
        async with db.begin_nested():
            await insert_earning(db, base["ride_id"], free["payment_id"], driver_id)  # the payment is free, the ride is not
    assert "uq_ride_earnings_ride_id" in str(error.value)
    assert await count(db, RideEarning) == 1

    # The allowed variations: 0 percent with a fee of 0, 100 percent with an earning of 0, and a cancellation.
    extra = [await settled_ride(rider, driver, with_earning=False) for _ in range(3)]
    await insert_earning(db, extra[0]["ride_id"], extra[0]["payment_id"], driver_id, commission_percent=0, platform_fee=0, driver_earning=1000)
    await insert_earning(db, extra[1]["ride_id"], extra[1]["payment_id"], driver_id, commission_percent=100, platform_fee=1000, driver_earning=0)
    await insert_earning(db, extra[2]["ride_id"], extra[2]["payment_id"], driver_id, kind="cancellation")
    await db.commit()
    assert await count(db, RideEarning) == 4


@pytest.mark.parametrize("percent, accepted", [(101, False), (-1, False), (0, True), (100, True)])
async def test_the_commission_of_a_pricing_rule_must_be_between_0_and_100(db, percent, accepted):
    if accepted:
        await set_rule(db, commission_percent=percent)
        assert await db.scalar(text("SELECT commission_percent FROM pricing_rules")) == percent
        return
    with pytest.raises(IntegrityError) as error:
        async with db.begin_nested():
            await db.execute(text("UPDATE pricing_rules SET commission_percent = :percent"), {"percent": percent})
    assert "ck_pricing_rules_commission_percent_range" in str(error.value)


# --- B. driver views ---


async def test_a_new_driver_has_all_zeros(client, driver):
    response = await get_summary(client, driver)
    assert response.status_code == 200 and response.json() == ZERO_SUMMARY


async def test_the_summary_adds_up_a_cash_trip_a_wallet_trip_and_a_wallet_cancellation_fee(
    client, db, admin, rider, driver, put_online, accept_offer
):
    await fund(client, admin, rider, 100000)
    cash = await start_ride(client, put_online, accept_offer, rider, driver, "cash")
    await go(client, cash, "arrive", "start")
    cash_fare = (await complete(client, cash)).json()["final_fare"]
    wallet = await start_ride(client, put_online, accept_offer, rider, driver, "wallet")
    await go(client, wallet, "arrive", "start")
    wallet_fare = (await complete(client, wallet)).json()["final_fare"]
    late = await start_ride(client, put_online, accept_offer, rider, driver, "wallet")
    await age_assignment(db, late["id"], 200)
    assert (await cancel(client, late)).json()["final_fare"] == 3000

    cash_fee, cash_earning = split(cash_fare)
    wallet_fee, wallet_earning = split(wallet_fare)
    assert split(3000) == (600, 2400)  # 3000 * 20 / 100 = 600, the driver keeps 2400
    body = (await get_summary(client, driver)).json()
    assert body == {
        "since": None, "until": None, "trips": 2, "cancellation_fees": 1,
        "total": {
            "rides": 3, "gross": cash_fare + wallet_fare + 3000, "platform_fee": cash_fee + wallet_fee + 600,
            "driver_earning": cash_earning + wallet_earning + 2400,
        },
        "cash": {"rides": 1, "gross": cash_fare, "platform_fee": cash_fee, "driver_earning": cash_earning},
        "wallet": {"rides": 2, "gross": wallet_fare + 3000, "platform_fee": wallet_fee + 600, "driver_earning": wallet_earning + 2400},
        # The platform collected the wallet fares and owes the driver its part; the driver collected the cash and owes the fee.
        "settlement": {
            "owed_to_driver": wallet_earning + 2400, "owed_by_driver": cash_fee, "net": wallet_earning + 2400 - cash_fee,
        },
    }


async def test_a_card_payment_counts_in_the_total_only(client, db, rider, driver, settled_ride):
    await settled_ride(rider, driver, amount=10000, method="card")
    await settled_ride(rider, driver, amount=5000, method="cash")
    body = (await get_summary(client, driver)).json()
    assert body["total"] == {"rides": 2, "gross": 15000, "platform_fee": 3000, "driver_earning": 12000}
    assert body["cash"] == {"rides": 1, "gross": 5000, "platform_fee": 1000, "driver_earning": 4000}
    assert body["wallet"] == ZERO
    assert body["settlement"] == {"owed_to_driver": 0, "owed_by_driver": 1000, "net": -1000}


@pytest_asyncio.fixture
async def three_hours(rider, driver, settled_ride):
    """Rows at T0, T0 + 1 h and T0 + 2 h, for 1000, 2000 and 3000 paise."""
    for hours, amount in enumerate((1000, 2000, 3000)):
        await settled_ride(rider, driver, amount=amount, created_at=T0 + timedelta(hours=hours))


def gross_in(body: dict) -> tuple[int, int]:
    return body["total"]["rides"], body["total"]["gross"]


async def test_since_is_inclusive_and_until_is_exclusive(client, driver, three_hours):
    one, two = T0 + timedelta(hours=1), T0 + timedelta(hours=2)
    assert gross_in((await get_summary(client, driver)).json()) == (3, 6000)
    assert gross_in((await get_summary(client, driver, since=one.isoformat())).json()) == (2, 5000)  # the row exactly at since is in
    assert gross_in((await get_summary(client, driver, until=one.isoformat())).json()) == (1, 1000)  # the row exactly at until is out
    both = (await get_summary(client, driver, since=one.isoformat(), until=two.isoformat())).json()
    assert gross_in(both) == (1, 2000)
    assert datetime.fromisoformat(both["since"]) == one and datetime.fromisoformat(both["until"]) == two  # echoed back
    empty = (await get_summary(client, driver, since=(T0 + timedelta(days=1)).isoformat())).json()
    assert empty["total"] == ZERO and empty["settlement"] == {"owed_to_driver": 0, "owed_by_driver": 0, "net": 0}


@pytest.mark.parametrize("path", ["/drivers/me/earnings", "/drivers/me/earnings/entries", "/admin/revenue", "/admin/drivers/{driver_id}/earnings"])
async def test_a_naive_time_or_an_empty_window_is_a_422(client, driver, admin, path):
    who = driver if path.startswith("/drivers") else admin
    path = path.format(driver_id=driver["driver"].id)
    assert (await get_summary(client, who, path, since="2026-03-01T12:00:00")).status_code == 422  # no timezone
    assert (await get_summary(client, who, path, until="2026-03-01T12:00:00")).status_code == 422
    assert (await get_summary(client, who, path, since="not a time")).status_code == 422
    same = T0.isoformat()
    for since, until in ((same, same), ((T0 + timedelta(hours=1)).isoformat(), same)):
        response = await get_summary(client, who, path, since=since, until=until)
        assert response.status_code == 422 and response.json()["detail"] == "until must be after since"


async def test_a_driver_sees_only_their_own_rows_and_the_roles_are_checked(client, db, rider, driver, admin, make_user, settled_ride):
    other = await make_user("driver")
    mine = await settled_ride(rider, driver, amount=1000)
    theirs = await settled_ride(rider, other, amount=7000)

    assert (await get_summary(client, driver)).json()["total"]["gross"] == 1000
    assert (await get_summary(client, other)).json()["total"]["gross"] == 7000
    assert [e["ride_id"] for e in (await get_summary(client, driver, "/drivers/me/earnings/entries")).json()] == [mine["ride_id"]]
    assert [e["ride_id"] for e in (await get_summary(client, other, "/drivers/me/earnings/entries")).json()] == [theirs["ride_id"]]

    for path in ("/drivers/me/earnings", "/drivers/me/earnings/entries"):
        assert (await get_summary(client, rider, path)).status_code == 403
        assert (await get_summary(client, admin, path)).status_code == 403
        assert (await client.get(path)).status_code == 401


async def test_a_driver_without_a_profile_gets_404_and_a_rejected_driver_can_read_their_earnings(client, db, rider, driver, settled_ride):
    nobody = User(role=UserRole.driver, name="No Profile", email="no-profile@example.com", password_hash="unused")
    db.add(nobody)
    await db.commit()
    headers = {"headers": {"Authorization": f"Bearer {create_access_token(nobody)}"}}
    for path in ("/drivers/me/earnings", "/drivers/me/earnings/entries"):
        response = await get_summary(client, headers, path)
        assert response.status_code == 404 and response.json()["detail"] == "You have no driver profile yet"

    await settled_ride(rider, driver, amount=1000)
    await db.execute(text("UPDATE drivers SET verification_status = 'rejected'"))
    await db.commit()
    assert (await get_summary(client, driver)).json()["total"]["gross"] == 1000
    assert len((await get_summary(client, driver, "/drivers/me/earnings/entries")).json()) == 1


async def test_the_entries_list_pages_newest_first_without_gaps_and_shows_no_rider_data(client, db, rider, driver, settled_ride):
    ride_ids = [(await settled_ride(rider, driver, amount=1000 + index, created_at=T0 + timedelta(minutes=index)))["ride_id"] for index in range(25)]
    newest_first = list(reversed(ride_ids))

    default = await get_summary(client, driver, "/drivers/me/earnings/entries")
    assert [e["ride_id"] for e in default.json()] == newest_first[:20]  # the default limit is 20
    first = default.json()[0]
    assert set(first) == ENTRY_KEYS
    assert (first["kind"], first["payment_method"], first["gross_amount"], first["commission_percent"]) == ("trip", "cash", 1024, 20)
    assert (first["platform_fee"], first["driver_earning"]) == split(1024)
    assert (first["pickup_address"], first["dropoff_address"], first["distance_m"], first["duration_s"]) == ("MG Road", "Koramangala", 5000, 900)
    assert "rider" not in default.text.lower() and rider["user"].email not in default.text  # no name, email or id of the rider

    seen, before_id = [], None
    while True:
        params = {"limit": 10} | ({"before_id": before_id} if before_id else {})
        page = (await get_summary(client, driver, "/drivers/me/earnings/entries", **params)).json()
        if not page:
            break
        seen += page
        before_id = page[-1]["id"]
    assert [e["ride_id"] for e in seen] == newest_first  # every row exactly once, newest first
    assert len({e["id"] for e in seen}) == 25 and [e["id"] for e in seen] == sorted((e["id"] for e in seen), reverse=True)

    since = (T0 + timedelta(minutes=10)).isoformat()
    until = (T0 + timedelta(minutes=5)).isoformat()
    assert len((await get_summary(client, driver, "/drivers/me/earnings/entries", since=since)).json()) == 15
    assert len((await get_summary(client, driver, "/drivers/me/earnings/entries", until=until)).json()) == 5

    for bad in (0, 101, -1, "abc"):
        assert (await get_summary(client, driver, "/drivers/me/earnings/entries", limit=bad)).status_code == 422, bad
    assert (await get_summary(client, driver, "/drivers/me/earnings/entries", limit=100)).status_code == 200


# --- C. admin views ---


async def test_the_revenue_is_the_sum_over_all_drivers_and_windows_work(client, db, rider, driver, admin, make_user, settled_ride):
    second, third = await make_user("driver"), await make_user("driver")
    # Worked out by hand at 20 percent: the fees are 2000, 4000, 600 and 1000.
    await settled_ride(rider, driver, amount=10000, method="cash", created_at=T0)
    await settled_ride(rider, second, amount=20000, method="wallet", created_at=T0 + timedelta(hours=1))
    await settled_ride(rider, second, amount=3000, method="cash", kind="cancellation", created_at=T0 + timedelta(hours=2))
    await settled_ride(rider, third, amount=5000, method="wallet", kind="cancellation", created_at=T0 + timedelta(hours=3))

    body = (await client.get("/admin/revenue", headers=admin["headers"])).json()
    assert body == {
        "since": None, "until": None, "trips": 2, "cancellation_fees": 2,
        "total": {"rides": 4, "gross": 38000, "platform_fee": 7600, "driver_earning": 30400},
        "cash": {"rides": 2, "gross": 13000, "platform_fee": 2600, "driver_earning": 10400},
        "wallet": {"rides": 2, "gross": 25000, "platform_fee": 5000, "driver_earning": 20000},
        "settlement": {"owed_to_driver": 20000, "owed_by_driver": 2600, "net": 17400},
    }

    window = {"since": (T0 + timedelta(hours=1)).isoformat(), "until": (T0 + timedelta(hours=3)).isoformat()}
    inside = (await client.get("/admin/revenue", params=window, headers=admin["headers"])).json()
    assert inside["total"] == {"rides": 2, "gross": 23000, "platform_fee": 4600, "driver_earning": 18400}

    for who in (rider, driver):
        assert (await client.get("/admin/revenue", headers=who["headers"])).status_code == 403
    assert (await client.get("/admin/revenue")).status_code == 401


async def test_an_admin_sees_a_drivers_summary_equal_to_the_drivers_own(client, rider, driver, admin, make_user, settled_ride):
    other = await make_user("driver")
    await settled_ride(rider, driver, amount=4000, method="wallet")
    await settled_ride(rider, driver, amount=1500, method="cash", kind="cancellation")
    await settled_ride(rider, other, amount=9000)

    own = (await get_summary(client, driver)).json()
    seen = await client.get(f"/admin/drivers/{driver['driver'].id}/earnings", headers=admin["headers"])
    assert seen.status_code == 200 and seen.json() == own and own["total"]["gross"] == 5500

    assert (await client.get("/admin/drivers/9999/earnings", headers=admin["headers"])).status_code == 404
    for who in (rider, driver):
        assert (await client.get(f"/admin/drivers/{driver['driver'].id}/earnings", headers=who["headers"])).status_code == 403
    assert (await client.get(f"/admin/drivers/{driver['driver'].id}/earnings")).status_code == 401


# --- D. receipts ---


async def vehicle_plate(db, driver: dict) -> str:
    return await db.scalar(select(Vehicle.plate_number).where(Vehicle.driver_id == driver["driver"].id))


async def test_the_receipt_of_a_tracked_wallet_trip_with_surge(client, db, fake_clock, wallet_trip):
    trip = wallet_trip
    # Surge was locked on the ride at request time: 1.5x, with the surged estimate.
    await db.execute(text("UPDATE rides SET surge_percent = 150, fare_estimate = 21000 WHERE id = :id"), {"id": trip["id"]})
    await db.commit()
    await go(client, trip, "arrive", "start")
    await walk(client, fake_clock, trip, [25 * i for i in range(40)])
    await set_times(db, trip["id"], 600)
    ride = (await complete(client, trip)).json()

    response = await get_receipt(client, trip["rider"], trip["id"])

    assert response.status_code == 200, response.text
    receipt = response.json()
    assert set(receipt) == RECEIPT_KEYS
    assert set(receipt["trip"]) == TRIP_KEYS and receipt["cancellation"] is None
    assert set(receipt["vehicle"]) == {"plate_number", "model", "color"}
    assert set(receipt["payment"]) == {"method", "amount", "status", "wallet_balance_after"}
    assert receipt["receipt_number"] == f"RCPT-{trip['id']:08d}" and len(receipt["receipt_number"]) == 13
    assert (receipt["kind"], receipt["status"], receipt["estimated_fare"]) == ("trip", "COMPLETED", 21000)
    assert (receipt["pickup_address"], receipt["dropoff_address"]) == (ride["pickup_address"], ride["dropoff_address"])

    fare = ride["final_fare"]
    trip_part = receipt["trip"]
    assert trip_part["total"] == fare == receipt["payment"]["amount"]
    assert trip_part["surge_percent"] == 150 and trip_part["surge_amount"] > 0
    assert trip_part["normal_fare"] + trip_part["surge_amount"] == trip_part["computed_fare"]
    assert (trip_part["distance_m"], trip_part["duration_s"]) == (ride["actual_distance_m"], ride["actual_duration_s"])
    assert trip_part["distance_source"] == "tracked" and trip_part["capped"] is False
    assert trip_part["base_fare"] + trip_part["distance_fare"] + trip_part["time_fare"] == trip_part["normal_fare"]

    entry = (await entries_of(db, trip["rider"]["user"].id))[-1]
    assert entry.kind == WalletEntryKind.RIDE_CHARGE and entry.ride_id == trip["id"]
    assert receipt["payment"] == {"method": "wallet", "amount": fare, "status": "succeeded", "wallet_balance_after": entry.balance_after}
    assert entry.balance_after == 50000 - fare
    assert receipt["driver_name"] == trip["driver"]["user"].name
    assert receipt["vehicle"] == {"plate_number": await vehicle_plate(db, trip["driver"]), "model": "Swift", "color": "white"}
    paid_at = await db.scalar(select(Payment.created_at).where(Payment.ride_id == trip["id"]))
    assert datetime.fromisoformat(receipt["issued_at"]) == paid_at
    assert datetime.fromisoformat(receipt["started_at"]) == datetime.fromisoformat(ride["started_at"])
    assert datetime.fromisoformat(receipt["ended_at"]) == datetime.fromisoformat(ride["completed_at"])


async def test_the_receipt_of_a_cash_trip_has_no_wallet_balance(client, cash_trip):
    await go(client, cash_trip, "arrive", "start")
    fare = (await complete(client, cash_trip)).json()["final_fare"]
    receipt = (await get_receipt(client, cash_trip["rider"], cash_trip["id"])).json()
    assert receipt["payment"] == {"method": "cash", "amount": fare, "status": "succeeded", "wallet_balance_after": None}


async def test_the_receipt_of_a_late_cancellation_shows_the_fee(client, db, wallet_trip):
    await age_assignment(db, wallet_trip["id"], 200)
    assert (await cancel(client, wallet_trip)).json()["final_fare"] == 3000

    receipt = (await get_receipt(client, wallet_trip["rider"], wallet_trip["id"])).json()

    assert set(receipt) == RECEIPT_KEYS and receipt["trip"] is None
    assert receipt["cancellation"] == {"fee": 3000, "reason": "late_cancellation", "cancelled_by": "rider"}
    assert (receipt["kind"], receipt["status"], receipt["started_at"]) == ("cancellation", "CANCELLED", None)
    assert receipt["payment"]["amount"] == 3000 and receipt["payment"]["method"] == "wallet"
    cancelled_at = await db.scalar(
        select(RideEvent.created_at).where(RideEvent.ride_id == wallet_trip["id"], RideEvent.to_status == RideStatus.CANCELLED)
    )
    assert datetime.fromisoformat(receipt["ended_at"]) == cancelled_at


async def test_there_is_no_receipt_for_a_free_cancellation(client, trip):
    assert (await cancel(client, trip)).json()["final_fare"] == 0
    response = await get_receipt(client, trip["rider"], trip["id"])
    assert response.status_code == 409 and response.json()["detail"] == NO_RECEIPT


async def test_there_is_no_receipt_for_a_driver_cancellation(client, db, trip):
    await age_assignment(db, trip["id"], 500)
    assert (await cancel(client, trip, "driver")).status_code == 200
    response = await get_receipt(client, trip["rider"], trip["id"])
    assert response.status_code == 409 and response.json()["detail"] == NO_RECEIPT


async def test_there_is_no_receipt_for_no_driver_found_a_requested_ride_or_a_trip_in_progress(client, new_requested, trip, make_user):
    nobody = await make_user("rider")
    created = await client.post("/rides", json=RIDE_BODY, headers=nobody["headers"])
    assert created.status_code == 201 and created.json()["status"] == "NO_DRIVER_FOUND"
    requested = await new_requested()
    await go(client, trip, "arrive", "start")

    for who, ride_id in ((nobody, created.json()["id"]), (requested["rider"], requested["id"]), (trip["rider"], trip["id"])):
        response = await get_receipt(client, who, ride_id)
        assert response.status_code == 409 and response.json()["detail"] == NO_RECEIPT


async def test_only_the_rider_of_the_ride_can_read_the_receipt(client, admin, make_user, cash_trip):
    await go(client, cash_trip, "arrive", "start")
    assert (await complete(client, cash_trip)).status_code == 200
    ride_id = cash_trip["id"]
    assert (await get_receipt(client, cash_trip["rider"], ride_id)).status_code == 200

    other = await make_user("rider")
    for response in (await get_receipt(client, other, ride_id), await get_receipt(client, other, 99999), await get_receipt(client, cash_trip["rider"], 99999)):
        assert response.status_code == 404 and response.json()["detail"] == "Ride not found"
    assert (await get_receipt(client, cash_trip["driver"], ride_id)).status_code == 403
    assert (await get_receipt(client, admin, ride_id)).status_code == 403
    assert (await client.get(f"/rides/{ride_id}/receipt")).status_code == 401


async def test_a_receipt_does_not_change_when_the_rule_or_surge_changes_later(client, db, wallet_trip):
    await go(client, wallet_trip, "arrive", "start")
    assert (await complete(client, wallet_trip)).status_code == 200
    before = await get_receipt(client, wallet_trip["rider"], wallet_trip["id"])

    await set_rule(db, base_fare=9999, per_km=1, per_min=1, min_fare=100, cancellation_fee=777, commission_percent=55, surge_cap=1.0)
    await pricing_repo.save_snapshot({"computed_at": 1, "zones": {"x": {"demand": 9, "supply": 0, "pressure_percent": 900, "surge_percent": 200}}}, 60)

    after = await get_receipt(client, wallet_trip["rider"], wallet_trip["id"])
    assert after.status_code == 200 and after.content == before.content


async def test_a_ride_settled_before_surge_existed_still_has_a_receipt(client, db, rider, driver, settled_ride):
    old = await settled_ride(rider, driver, amount=9000, old_breakdown=True)
    stored = await db.scalar(select(Ride.fare_breakdown).where(Ride.id == old["ride_id"]))
    assert not {"normal_fare", "surge_percent", "surge_amount"} & set(stored)

    receipt = (await get_receipt(client, rider, old["ride_id"])).json()

    assert receipt["trip"]["surge_percent"] == 100 and receipt["trip"]["surge_amount"] == 0
    assert receipt["trip"]["normal_fare"] == receipt["trip"]["computed_fare"] == 9000
    assert receipt["trip"]["total"] == 9000


async def test_a_receipt_says_nothing_about_commission_ids_or_contact_data(client, db, wallet_trip):
    await go(client, wallet_trip, "arrive", "start")
    assert (await complete(client, wallet_trip)).status_code == 200
    response = await get_receipt(client, wallet_trip["rider"], wallet_trip["id"])
    text_of_receipt = response.text.lower()
    driver = wallet_trip["driver"]

    assert "otp" not in response.json()
    for word in ("commission", "platform_fee", "driver_earning", "earning", "driver_id", "otp"):
        assert word not in text_of_receipt, word
    for private in (driver["user"].email, driver["driver"].license_number, driver["user"].phone or "no-phone-here"):
        assert private.lower() not in text_of_receipt, private
    assert json.loads(response.text)["driver_name"] == driver["user"].name
