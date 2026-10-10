import asyncio
import base64
import hashlib
import hmac
import json
import logging
import time
import types
import uuid
from urllib.parse import parse_qs

import httpx
import pytest
import pytest_asyncio
from fastapi import HTTPException
from redis.exceptions import RedisError
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError

from app.config import settings
from app.models import Payment, Ride, RideEvent, RideStatus, StripeEvent, TopupStatus, Wallet, WalletEntry, WalletEntryKind, WalletTopup
from app.repositories import rides as rides_repo
from app.services import payments as payments_service
from app.services.payments import call_stripe as real_call_stripe
from app.services import pricing
from app.services import wallet as wallet_service
from test_edge_cases import logged  # noqa: F401  (a fixture: caplog attached to uvicorn's logger)
from test_fares import age_assignment, complete, go, set_times, walk
from test_rides import RIDE_BODY

REAL_ASYNC_CLIENT = httpx.AsyncClient  # before any test replaces it
SECRET_KEY = "sk_test_unit_test_key"
WEBHOOK_SECRET = "whsec_unit_test_secret"
# The fake route of the suite: 5000 m, 900 s, estimate 14000, so the fare cap is 21000 (written out by hand).
ESTIMATE = 14000
CAP = 21000
WALLET_402 = "Your wallet balance (₹{balance}) is below the ₹210.00 this trip can cost. Add money or pay with cash."


# --- helpers and fixtures ---


@pytest.fixture
def stripe_configured(monkeypatch):
    """Test keys in the settings and a controllable stand-in for call_stripe: it records every call and answers with a
    Checkout Session. The same Stripe idempotency key gives the same session, like the real thing."""
    monkeypatch.setattr(settings, "stripe_secret_key", SECRET_KEY)
    monkeypatch.setattr(settings, "stripe_webhook_secret", WEBHOOK_SECRET)
    monkeypatch.setattr(settings, "stripe_api_url", "http://stripe.test")
    monkeypatch.setattr(settings, "app_base_url", "http://app.test")
    stripe = types.SimpleNamespace(calls=[], sessions={}, delay=0.0, fail=None)

    async def call_stripe(method, path, form=None, idempotency_key=None):
        stripe.calls.append({"method": method, "path": path, "form": form, "key": idempotency_key})
        if stripe.delay:
            await asyncio.sleep(stripe.delay)
        if stripe.fail == "409":
            raise HTTPException(status_code=409, detail="Another request with this idempotency key is in progress. Retry in a moment.")
        if stripe.fail:
            raise HTTPException(status_code=502, detail="Card payments are unavailable")
        if method == "GET":
            return dict(stripe.sessions[path.rsplit("/", 1)[1]])
        session_id = "cs_test_" + idempotency_key
        stripe.sessions.setdefault(session_id, {
            "id": session_id, "object": "checkout.session", "url": f"https://checkout.test/{session_id}",
            "amount_total": int(form["line_items[0][price_data][unit_amount]"]), "currency": "inr",
            "payment_status": "unpaid", "status": "open", "payment_intent": None,
            "client_reference_id": str(form["client_reference_id"]),
        })
        return dict(stripe.sessions[session_id])

    monkeypatch.setattr(payments_service, "call_stripe", call_stripe)
    return stripe


def sign(body: bytes, secret: str = WEBHOOK_SECRET, timestamp: int | None = None) -> str:
    """A valid Stripe-Signature header for this exact body."""
    timestamp = int(time.time()) if timestamp is None else timestamp
    return f"t={timestamp},v1=" + hmac.new(secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256).hexdigest()


def event_body(event_type: str, session: dict, event_id: str | None = None) -> bytes:
    event = {"id": event_id or "evt_test_" + uuid.uuid4().hex, "object": "event", "type": event_type, "data": {"object": session}}
    return json.dumps(event).encode()


async def post_webhook(client, body: bytes, header: str | None = "sign"):
    headers = {"Content-Type": "application/json"}
    if header is not None:
        headers["Stripe-Signature"] = sign(body) if header == "sign" else header
    return await client.post("/webhooks/stripe", content=body, headers=headers)


def keyed(who: dict, key: str | None = None) -> dict:
    return {**who["headers"], "Idempotency-Key": key or "key-" + uuid.uuid4().hex[:16]}


async def fund(client, admin, rider: dict, paise: int) -> dict:
    response = await client.post(
        f"/admin/wallets/{rider['user'].id}/adjust", json={"amount": paise, "note": "test funding"}, headers=keyed(admin)
    )
    assert response.status_code == 201, response.text
    return response.json()


async def get_wallet(client, rider: dict) -> dict:
    response = await client.get("/wallet", headers=rider["headers"])
    assert response.status_code == 200, response.text
    return response.json()


async def balance_of(db, user_id: int) -> int:
    return (await db.execute(select(Wallet.balance).where(Wallet.user_id == user_id))).scalar_one_or_none() or 0


async def entries_of(db, user_id: int):
    result = await db.execute(
        select(WalletEntry.id, WalletEntry.amount, WalletEntry.kind, WalletEntry.balance_after, WalletEntry.ride_id, WalletEntry.topup_id)
        .where(WalletEntry.user_id == user_id).order_by(WalletEntry.id)
    )
    return result.all()


async def count(db, model, *where) -> int:
    return await db.scalar(select(func.count()).select_from(model).where(*where))


async def payments_of(db, ride_id: int):
    result = await db.execute(
        select(Payment.amount, Payment.method.label("method"), Payment.status, Payment.idempotency_key).where(Payment.ride_id == ride_id)
    )
    return result.all()


async def topup_state(db, topup_id: int):
    result = await db.execute(
        select(WalletTopup.status, WalletTopup.stripe_session_id, WalletTopup.stripe_payment_intent_id, WalletTopup.completed_at)
        .where(WalletTopup.id == topup_id)
    )
    return result.one()


async def new_topup(client, rider: dict, amount: int = 30000, key: str | None = None):
    return await client.post("/wallet/topups", json={"amount": amount}, headers=keyed(rider, key))


async def paid_topup(client, db, stripe, rider: dict, amount: int = 30000):
    """A top-up whose Checkout Session is paid at Stripe. Returns (topup id, the session as the webhook would carry it)."""
    created = await new_topup(client, rider, amount)
    assert created.status_code == 201, created.text
    topup_id = created.json()["id"]
    session = stripe.sessions[(await topup_state(db, topup_id)).stripe_session_id]
    session.update(payment_status="paid", status="complete", payment_intent=f"pi_test_{topup_id}")
    return topup_id, dict(session)


async def start_ride(client, put_online, accept_offer, rider: dict, driver: dict, method: str = "cash") -> dict:
    """A DRIVER_ASSIGNED ride paid with `method` (the wallet must already hold the money)."""
    await put_online(driver, RIDE_BODY["pickup_lat"], RIDE_BODY["pickup_lng"])
    created = await client.post("/rides", json={**RIDE_BODY, "payment_method": method}, headers=rider["headers"])
    assert created.status_code == 201, created.text
    ride = await accept_offer(driver)
    assert ride["status"] == "DRIVER_ASSIGNED" and ride["payment_method"] == method
    return {"rider": rider, "driver": driver, "id": ride["id"]}


@pytest_asyncio.fixture
async def wallet_trip(client, db, admin, rider, driver, put_online, accept_offer):
    """A DRIVER_ASSIGNED wallet ride; the rider's wallet holds exactly 50000 paise before it."""
    await fund(client, admin, rider, 50000)
    return await start_ride(client, put_online, accept_offer, rider, driver, "wallet")


@pytest_asyncio.fixture
async def cash_trip(client, rider, driver, put_online, accept_offer):
    return await start_ride(client, put_online, accept_offer, rider, driver, "cash")


# --- A. ledger and wallet ---


async def test_a_new_rider_has_an_empty_wallet_and_reading_creates_no_row(client, db, rider, driver, admin):
    assert await get_wallet(client, rider) == {"balance": 0, "reserved": 0, "available": 0}
    assert (await client.get("/wallet/entries", headers=rider["headers"])).json() == []
    assert (await client.get("/wallet/topups", headers=rider["headers"])).json() == []
    assert await count(db, Wallet) == 0

    assert (await client.get("/wallet", headers=driver["headers"])).status_code == 403
    assert (await client.get("/wallet", headers=admin["headers"])).status_code == 403
    assert (await client.get("/wallet")).status_code == 401
    assert (await client.get("/wallet/entries", headers=driver["headers"])).status_code == 403
    assert (await client.get("/wallet/entries")).status_code == 401


async def test_an_admin_adjustment_is_one_ledger_entry_and_replays_return_it(client, db, admin, rider, make_user):
    key = "adjust-key-1"
    body = {"amount": 5000, "note": "goodwill"}
    created = await client.post(f"/admin/wallets/{rider['user'].id}/adjust", json=body, headers=keyed(admin, key))
    assert created.status_code == 201
    entry = created.json()
    assert set(entry) == {"id", "amount", "kind", "balance_after", "ride_id", "topup_id", "note", "created_at"}  # no actor id
    assert (entry["amount"], entry["kind"], entry["balance_after"], entry["note"]) == (5000, "ADJUSTMENT", 5000, "goodwill")
    stored = (await db.execute(select(WalletEntry.actor_user_id, WalletEntry.idempotency_key).where(WalletEntry.id == entry["id"]))).one()
    assert (stored.actor_user_id, stored.idempotency_key) == (admin["user"].id, key)
    assert (await get_wallet(client, rider))["balance"] == 5000

    replay = await client.post(f"/admin/wallets/{rider['user'].id}/adjust", json=body, headers=keyed(admin, key))
    assert replay.status_code == 200 and replay.json()["id"] == entry["id"]
    assert (await get_wallet(client, rider))["balance"] == 5000
    assert await count(db, WalletEntry) == 1

    for different in ({"amount": 6000, "note": "goodwill"}, {"amount": 5000, "note": "other"}):
        refused = await client.post(f"/admin/wallets/{rider['user'].id}/adjust", json=different, headers=keyed(admin, key))
        assert refused.status_code == 409
        assert refused.json()["detail"] == "This idempotency key was already used with a different request"

    other = await make_user("rider")
    separate = await client.post(f"/admin/wallets/{other['user'].id}/adjust", json=body, headers=keyed(admin, key))
    assert separate.status_code == 201 and separate.json()["id"] != entry["id"]
    assert await count(db, WalletEntry) == 2


async def test_adjustment_validation_roles_and_targets(client, db, admin, rider, driver, make_user):
    url = f"/admin/wallets/{rider['user'].id}/adjust"
    good = {"amount": 1000, "note": "ok"}
    for headers in (admin["headers"], {**admin["headers"], "Idempotency-Key": "short"}, {**admin["headers"], "Idempotency-Key": "bad key!!!!!"},
                    {**admin["headers"], "Idempotency-Key": "x" * 65}):
        assert (await client.post(url, json=good, headers=headers)).status_code == 422
    for amount in (0, 1000001, -1000001, "1000", 10.5, 10.0, True, None):
        assert (await client.post(url, json={"amount": amount, "note": "n"}, headers=keyed(admin))).status_code == 422, amount
    for note in ("", "   ", "x" * 201, None):
        assert (await client.post(url, json={"amount": 100, "note": note}, headers=keyed(admin))).status_code == 422, note
    assert (await client.post(url, json={"amount": 1000000, "note": "max"}, headers=keyed(admin))).status_code == 201
    assert (await client.post(url, json={"amount": 100, "note": "x" * 200}, headers=keyed(admin))).status_code == 201
    assert (await client.post(url, json={"amount": 100, "note": "  padded  "}, headers=keyed(admin))).json()["note"] == "padded"
    assert await count(db, WalletEntry) == 3
    assert (await client.post(url, json={"amount": -1000000, "note": "debit"}, headers=keyed(admin))).status_code == 201

    balance = await balance_of(db, rider["user"].id)
    assert balance == 200
    too_much = await client.post(url, json={"amount": -(balance + 1), "note": "debit"}, headers=keyed(admin))
    assert too_much.status_code == 409 and too_much.json()["detail"] == "Adjustment would make the balance negative"
    assert await balance_of(db, rider["user"].id) == balance and await count(db, WalletEntry) == 4
    assert (await client.post(url, json={"amount": -balance, "note": "all of it"}, headers=keyed(admin))).status_code == 201
    assert await balance_of(db, rider["user"].id) == 0

    for target in (driver["user"].id, admin["user"].id, 99999):
        assert (await client.post(f"/admin/wallets/{target}/adjust", json=good, headers=keyed(admin))).status_code == 404
    assert (await client.post(url, json=good, headers=keyed(rider))).status_code == 403
    assert (await client.post(url, json=good, headers=keyed(driver))).status_code == 403
    assert (await client.post(url, json=good, headers={"Idempotency-Key": "key-no-token-1"})).status_code == 401


async def test_the_entries_list_pages_newest_first_and_shows_only_your_own(client, admin, rider, make_user):
    other = await make_user("rider")
    await fund(client, admin, other, 777)
    for amount in (1000, 2000, -500, 300, 100):
        response = await client.post(
            f"/admin/wallets/{rider['user'].id}/adjust", json={"amount": amount, "note": "n"}, headers=keyed(admin)
        )
        assert response.status_code == 201

    everything = (await client.get("/wallet/entries", headers=rider["headers"])).json()
    assert [entry["amount"] for entry in everything] == [100, 300, -500, 2000, 1000]
    assert all(entry["note"] == "n" for entry in everything)
    ascending = list(reversed(everything))
    for before, after in zip(ascending, ascending[1:]):
        assert after["balance_after"] == before["balance_after"] + after["amount"]
    assert ascending[0]["balance_after"] == ascending[0]["amount"] and everything[0]["balance_after"] == 2900

    first = (await client.get("/wallet/entries?limit=2", headers=rider["headers"])).json()
    second = (await client.get(f"/wallet/entries?limit=2&before_id={first[-1]['id']}", headers=rider["headers"])).json()
    last = (await client.get(f"/wallet/entries?limit=2&before_id={second[-1]['id']}", headers=rider["headers"])).json()
    assert [e["id"] for e in first + second + last] == [e["id"] for e in everything]
    for limit in (0, 101, -1):
        assert (await client.get(f"/wallet/entries?limit={limit}", headers=rider["headers"])).status_code == 422
    assert (await client.get("/wallet/entries?limit=100", headers=rider["headers"])).status_code == 200


async def test_twenty_simultaneous_adjustments_give_an_exact_balance_and_one_key_gives_one_entry(client, db, admin, rider):
    url = f"/admin/wallets/{rider['user'].id}/adjust"
    answers = await asyncio.gather(*[client.post(url, json={"amount": 100, "note": "burst"}, headers=keyed(admin)) for _ in range(20)])
    assert [a.status_code for a in answers] == [201] * 20
    entries = await entries_of(db, rider["user"].id)
    assert len(entries) == 20 and await balance_of(db, rider["user"].id) == 2000
    assert len({e.balance_after for e in entries}) == 20
    assert [e.balance_after for e in entries] == [100 * (index + 1) for index in range(20)]  # entry ids follow lock order

    same = keyed(admin, "one-key-for-all")
    answers = await asyncio.gather(*[client.post(url, json={"amount": 50, "note": "same"}, headers=same) for _ in range(10)])
    assert sorted(a.status_code for a in answers) == [200] * 9 + [201]
    assert len({a.json()["id"] for a in answers}) == 1
    assert await count(db, WalletEntry, WalletEntry.idempotency_key == "one-key-for-all") == 1
    assert await balance_of(db, rider["user"].id) == 2050


# --- B. ride payments ---


async def test_a_completed_wallet_ride_is_charged_from_the_wallet(client, db, fake_clock, wallet_trip):
    trip = wallet_trip
    await go(client, trip, "arrive", "start")
    await walk(client, fake_clock, trip, [25 * i for i in range(40)])
    await set_times(db, trip["id"], 600)
    response = await complete(client, trip)

    assert response.status_code == 200
    ride = response.json()
    fare = ride["final_fare"]
    assert ride["payment_method"] == "wallet" and fare > 0
    payments = await payments_of(db, trip["id"])
    assert len(payments) == 1
    assert (payments[0].amount, payments[0].method.value, payments[0].status.value, payments[0].idempotency_key) == (
        fare, "wallet", "succeeded", f"ride:{trip['id']}:charge"
    )
    entries = await entries_of(db, trip["rider"]["user"].id)
    assert [(e.amount, e.kind, e.ride_id) for e in entries[1:]] == [(-fare, WalletEntryKind.RIDE_CHARGE, trip["id"])]
    assert entries[-1].balance_after == 50000 - fare
    assert await balance_of(db, trip["rider"]["user"].id) == 50000 - fare
    assert fare <= pricing.fare_cap(ESTIMATE)


async def test_a_completed_cash_ride_has_a_cash_payment_and_no_ledger_entry(client, db, cash_trip):
    trip = cash_trip
    await go(client, trip, "arrive", "start")
    ride = (await complete(client, trip)).json()
    payments = await payments_of(db, trip["id"])
    assert [(p.amount, p.method.value, p.status.value) for p in payments] == [(ride["final_fare"], "cash", "succeeded")]
    assert await count(db, WalletEntry) == 0 and await count(db, Wallet) == 0
    assert ride["payment_method"] == "cash"


async def test_cancellation_fees_are_charged_by_the_ride_method(client, db, admin, rider, driver, make_user, put_online, accept_offer):
    # Wallet ride cancelled late: the fee comes out of the wallet.
    await fund(client, admin, rider, 50000)
    late = await start_ride(client, put_online, accept_offer, rider, driver, "wallet")
    await age_assignment(db, late["id"], 200)
    assert (await client.post(f"/rides/{late['id']}/cancel", headers=rider["headers"])).status_code == 200
    assert [(p.amount, p.method.value) for p in await payments_of(db, late["id"])] == [(3000, "wallet")]
    assert [(e.amount, e.ride_id) for e in (await entries_of(db, rider["user"].id))[1:]] == [(-3000, late["id"])]
    assert await balance_of(db, rider["user"].id) == 47000

    # Within the free window: fee 0, nothing is created. The same for a cancel by the driver.
    free = await start_ride(client, put_online, accept_offer, rider, driver, "wallet")
    assert (await client.post(f"/rides/{free['id']}/cancel", headers=rider["headers"])).status_code == 200
    by_driver = await start_ride(client, put_online, accept_offer, rider, driver, "wallet")
    await age_assignment(db, by_driver["id"], 200)
    assert (await client.post(f"/rides/{by_driver['id']}/cancel", headers=driver["headers"])).status_code == 200
    for ride_id in (free["id"], by_driver["id"]):
        assert await payments_of(db, ride_id) == []
        assert (await db.execute(select(Ride.final_fare).where(Ride.id == ride_id))).scalar_one() == 0
    assert await count(db, WalletEntry, WalletEntry.kind == WalletEntryKind.RIDE_CHARGE) == 1
    assert await balance_of(db, rider["user"].id) == 47000

    # NO_DRIVER_FOUND (nobody online): the ride keeps a NULL fare and creates nothing.
    nobody = await client.post("/drivers/me/offline", headers=driver["headers"])
    assert nobody.status_code == 200
    none_found = await client.post("/rides", json={**RIDE_BODY, "payment_method": "wallet"}, headers=rider["headers"])
    assert none_found.json()["status"] == "NO_DRIVER_FOUND"
    assert await payments_of(db, none_found.json()["id"]) == [] and none_found.json()["final_fare"] is None

    # A cash ride with a fee: a cash payment, no entry.
    cash_rider = await make_user("rider")
    cash = await start_ride(client, put_online, accept_offer, cash_rider, driver, "cash")
    await age_assignment(db, cash["id"], 200)
    assert (await client.post(f"/rides/{cash['id']}/cancel", headers=cash_rider["headers"])).status_code == 200
    assert [(p.amount, p.method.value) for p in await payments_of(db, cash["id"])] == [(3000, "cash")]
    assert await entries_of(db, cash_rider["user"].id) == []


async def test_the_wallet_must_cover_the_most_the_trip_can_cost(client, db, admin, rider, driver, make_user, put_online, monkeypatch):
    await put_online(driver, RIDE_BODY["pickup_lat"], RIDE_BODY["pickup_lng"])
    await put_online(await make_user("driver"), RIDE_BODY["pickup_lat"], RIDE_BODY["pickup_lng"])
    estimate = await client.post("/rides/estimate", json={k: v for k, v in RIDE_BODY.items() if "address" not in k}, headers=rider["headers"])
    assert estimate.json()["fare_estimate"] == ESTIMATE and estimate.json()["max_fare"] == CAP

    body = {**RIDE_BODY, "payment_method": "wallet"}
    refused = await client.post("/rides", json=body, headers=rider["headers"])
    assert refused.status_code == 402
    assert refused.json()["detail"] == WALLET_402.format(balance="0.00")
    assert await count(db, Ride) == 0

    exact, short = await make_user("rider"), await make_user("rider")
    await fund(client, admin, exact, CAP)
    await fund(client, admin, short, CAP - 1)
    assert (await client.post("/rides", json=body, headers=short["headers"])).status_code == 402
    assert (await client.post("/rides", json=body, headers=exact["headers"])).status_code == 201
    assert await count(db, Ride) == 1

    # Cash always works, and so does leaving the field out.
    assert (await client.post("/rides", json={**RIDE_BODY, "payment_method": "cash"}, headers=rider["headers"])).status_code == 201
    other = await make_user("rider")
    assert (await client.post("/rides", json=RIDE_BODY, headers=other["headers"])).json()["payment_method"] == "cash"
    for bad in ("card", "bitcoin", 5, None):
        fresh = await make_user("rider")
        assert (await client.post("/rides", json={**RIDE_BODY, "payment_method": bad}, headers=fresh["headers"])).status_code == 422

    # The checks run in a fixed order: the active-ride 409, then the stale price 409, and only then the money.
    assert (await client.post("/rides", json=body, headers=exact["headers"])).status_code == 409
    assert (await client.post("/rides", json=body, headers=rider["headers"])).status_code == 409

    async def surging(db, lat, lng, vehicle_type="economy"):
        return "u0abc", 150

    monkeypatch.setattr(pricing, "get_surge_percent", surging)
    poor = await make_user("rider")
    stale = await client.post("/rides", json={**body, "accepted_surge_percent": 100}, headers=poor["headers"])
    assert stale.status_code == 409 and stale.json()["detail"].startswith("Prices have increased")
    still_402 = await client.post("/rides", json={**body, "accepted_surge_percent": 150}, headers=poor["headers"])
    assert still_402.status_code == 402


async def test_the_reserved_amount_is_the_cap_of_the_active_wallet_ride(client, db, admin, rider, driver, put_online, accept_offer):
    await fund(client, admin, rider, 30000)
    assert await get_wallet(client, rider) == {"balance": 30000, "reserved": 0, "available": 30000}

    trip = await start_ride(client, put_online, accept_offer, rider, driver, "wallet")
    assert await get_wallet(client, rider) == {"balance": 30000, "reserved": CAP, "available": 30000 - CAP}
    await go(client, trip, "arrive", "start", "complete")
    wallet = await get_wallet(client, rider)
    assert wallet["reserved"] == 0 and wallet["available"] == wallet["balance"] < 30000

    cash = await start_ride(client, put_online, accept_offer, rider, driver, "cash")
    assert (await get_wallet(client, rider))["reserved"] == 0
    assert (await client.post(f"/rides/{cash['id']}/cancel", headers=rider["headers"])).status_code == 200


async def test_a_failure_while_charging_undoes_the_whole_settlement(client, db, monkeypatch, wallet_trip):
    trip = wallet_trip
    real = wallet_service.post_entry
    await go(client, trip, "arrive", "start")

    async def broken(*args, **kwargs):
        raise RuntimeError("the ledger is broken")

    monkeypatch.setattr(wallet_service, "post_entry", broken)
    assert (await complete(client, trip)).status_code == 500
    stored = (await db.execute(select(Ride.status, Ride.final_fare, Ride.fare_breakdown).where(Ride.id == trip["id"]))).one()
    assert (stored.status, stored.final_fare, stored.fare_breakdown) == (RideStatus.IN_PROGRESS, None, None)
    assert await payments_of(db, trip["id"]) == []
    assert await count(db, WalletEntry, WalletEntry.kind == WalletEntryKind.RIDE_CHARGE) == 0
    assert await count(db, RideEvent, RideEvent.ride_id == trip["id"], RideEvent.to_status == RideStatus.COMPLETED) == 0

    monkeypatch.setattr(wallet_service, "post_entry", real)
    assert (await complete(client, trip)).status_code == 200
    assert len(await payments_of(db, trip["id"])) == 1
    assert await count(db, WalletEntry, WalletEntry.kind == WalletEntryKind.RIDE_CHARGE) == 1


async def test_a_failure_while_charging_a_cancellation_fee_undoes_the_cancel(client, db, monkeypatch, wallet_trip):
    trip = wallet_trip
    real = wallet_service.post_entry
    await age_assignment(db, trip["id"], 200)

    async def broken(*args, **kwargs):
        raise RuntimeError("the ledger is broken")

    monkeypatch.setattr(wallet_service, "post_entry", broken)
    assert (await client.post(f"/rides/{trip['id']}/cancel", headers=trip["rider"]["headers"])).status_code == 500
    stored = (await db.execute(select(Ride.status, Ride.final_fare).where(Ride.id == trip["id"]))).one()
    assert (stored.status, stored.final_fare) == (RideStatus.DRIVER_ASSIGNED, None)
    assert await payments_of(db, trip["id"]) == []

    monkeypatch.setattr(wallet_service, "post_entry", real)
    assert (await client.post(f"/rides/{trip['id']}/cancel", headers=trip["rider"]["headers"])).status_code == 200
    assert len(await payments_of(db, trip["id"])) == 1


async def test_two_simultaneous_completions_charge_once(client, db, wallet_trip):
    trip = wallet_trip
    await go(client, trip, "arrive", "start")
    answers = await asyncio.gather(complete(client, trip), complete(client, trip))
    assert sorted(a.status_code for a in answers) == [200, 409]
    assert len(await payments_of(db, trip["id"])) == 1
    assert await count(db, WalletEntry, WalletEntry.kind == WalletEntryKind.RIDE_CHARGE) == 1


async def test_completing_without_redis_still_charges(client, db, monkeypatch, wallet_trip):
    trip = wallet_trip
    await go(client, trip, "arrive", "start")

    async def down(ride_id):
        raise RedisError("down")

    monkeypatch.setattr(rides_repo, "get_trip", down)
    ride = (await complete(client, trip)).json()
    assert ride["fare_breakdown"]["fallback_reason"] == "no_tracking"
    assert [p.amount for p in await payments_of(db, trip["id"])] == [ride["final_fare"]]
    assert await balance_of(db, trip["rider"]["user"].id) == 50000 - ride["final_fare"]


async def violates(db, constraint: str, sql: str, **params) -> None:
    """Runs one insert that the database must refuse, and checks that the error names the constraint."""
    with pytest.raises(IntegrityError) as error:
        await db.execute(text(sql), params)
    await db.rollback()
    assert constraint in str(error.value), str(error.value)


async def test_the_database_refuses_double_payments_and_inconsistent_entries(db, rider, make_user, insert_ride):
    first, second = await insert_ride(rider, RideStatus.COMPLETED), await insert_ride(await make_user("rider"), RideStatus.COMPLETED)
    # Plain numbers: a rollback expires the ORM objects, and reading them again would need I/O.
    user, ride_id, other_id, other_rider = rider["user"].id, first.id, second.id, second.rider_id
    payment = "INSERT INTO payments (ride_id, amount, method, status, idempotency_key) VALUES (:ride, :amount, 'cash', 'succeeded', :key)"
    entry = (
        "INSERT INTO wallet_entries (user_id, amount, kind, balance_after, ride_id, topup_id, idempotency_key) "
        "VALUES (:user, :amount, :kind, 0, :ride, :topup, :key)"
    )
    topup = (await db.execute(text(
        "INSERT INTO wallet_topups (user_id, amount, status, idempotency_key) VALUES (:user, 10000, 'PENDING', 'topup-key-1') RETURNING id"
    ), {"user": user})).scalar_one()
    await db.execute(text(payment), {"ride": ride_id, "amount": 100, "key": "ride:a"})
    await db.execute(text(entry), {"user": user, "amount": -100, "kind": "RIDE_CHARGE", "ride": ride_id, "topup": None, "key": None})
    await db.execute(text(entry), {"user": user, "amount": 10000, "kind": "TOPUP", "ride": None, "topup": topup, "key": None})
    await db.execute(text(entry), {"user": user, "amount": 5, "kind": "ADJUSTMENT", "ride": None, "topup": None, "key": "adjust-key-1"})
    await db.commit()

    await violates(db, "uq_payments_ride_id", payment, ride=ride_id, amount=100, key="ride:b")
    await violates(db, "uq_payments_idempotency_key", payment, ride=other_id, amount=100, key="ride:a")
    await violates(db, "ck_payments_amount_positive", payment, ride=other_id, amount=0, key="ride:c")
    await violates(db, "ck_payments_amount_positive", payment, ride=other_id, amount=-5, key="ride:c")
    base = {"user": user, "amount": -100, "kind": "RIDE_CHARGE", "ride": other_id, "topup": None, "key": None}
    await violates(db, "uq_wallet_entries_one_charge_per_ride", entry, **{**base, "ride": ride_id})
    await violates(db, "uq_wallet_entries_one_credit_per_topup", entry, **{**base, "amount": 10000, "kind": "TOPUP", "ride": None, "topup": topup})
    await violates(db, "ck_wallet_entries_ride_id_matches_kind", entry, **{**base, "ride": None})
    await violates(db, "ck_wallet_entries_ride_id_matches_kind", entry, **{**base, "kind": "ADJUSTMENT"})
    await violates(db, "ck_wallet_entries_topup_id_matches_kind", entry, **{**base, "amount": 5, "kind": "TOPUP", "ride": None})
    await violates(db, "ck_wallet_entries_topup_id_matches_kind", entry, **{**base, "kind": "ADJUSTMENT", "ride": None, "topup": topup})
    await violates(db, "ck_wallet_entries_amount_sign_matches_kind", entry, **{**base, "amount": 100})
    await violates(db, "ck_wallet_entries_amount_sign_matches_kind", entry, **{**base, "amount": -100, "kind": "TOPUP", "ride": None, "topup": topup})
    await violates(db, "ck_wallet_entries_amount_nonzero", entry, **{**base, "amount": 0, "kind": "ADJUSTMENT", "ride": None})
    await violates(db, "uq_wallet_entries_user_id_idempotency_key", entry, **{**base, "amount": 5, "kind": "ADJUSTMENT", "ride": None, "key": "adjust-key-1"})

    # The allowed variations: another user may reuse a key, a NULL key may repeat, a debit with an adjustment kind is fine.
    await db.execute(text(entry), {**base, "amount": -7, "kind": "ADJUSTMENT", "ride": None})
    await db.execute(text(entry), {**base, "amount": -7, "kind": "ADJUSTMENT", "ride": None})
    await db.execute(text(entry), {**base, "user": other_rider, "amount": 5, "kind": "ADJUSTMENT", "ride": None, "key": "adjust-key-1"})
    await db.execute(text(payment), {"ride": other_id, "amount": 1, "key": "ride:d"})
    await db.commit()


async def test_a_capped_trip_never_costs_more_than_the_reserved_balance(client, db, admin, rider, driver, put_online, accept_offer):
    await fund(client, admin, rider, CAP)  # exactly the cap
    trip = await start_ride(client, put_online, accept_offer, rider, driver, "wallet")
    await go(client, trip, "arrive", "start")
    await set_times(db, trip["id"], 3 * 3600)  # a three hour trip: the computed fare is far above the cap
    ride = (await complete(client, trip)).json()
    assert ride["fare_breakdown"]["capped"] is True and ride["final_fare"] == CAP
    assert await balance_of(db, rider["user"].id) == 0


async def test_a_cancellation_fee_above_the_balance_makes_it_negative_and_blocks_wallet_rides(
    client, db, admin, rider, driver, put_online, accept_offer
):
    await db.execute(text("UPDATE pricing_rules SET cancellation_fee = 50000"))
    await db.commit()
    await fund(client, admin, rider, CAP)
    trip = await start_ride(client, put_online, accept_offer, rider, driver, "wallet")
    await age_assignment(db, trip["id"], 200)
    assert (await client.post(f"/rides/{trip['id']}/cancel", headers=rider["headers"])).status_code == 200

    entries = await entries_of(db, rider["user"].id)
    assert [e.amount for e in entries] == [CAP, -50000]
    assert await balance_of(db, rider["user"].id) == sum(e.amount for e in entries) == -29000
    assert entries[-1].balance_after == -29000
    refused = await client.post("/rides", json={**RIDE_BODY, "payment_method": "wallet"}, headers=rider["headers"])
    assert refused.status_code == 402
    assert (await client.post("/rides", json=RIDE_BODY, headers=rider["headers"])).status_code == 201  # cash still works


# --- C. creating top-ups ---


async def test_stripe_not_configured_or_a_live_key_is_refused_before_any_row_is_written(client, db, rider, monkeypatch, logged):
    for key in ("", "sk_live_unit_test_secret_value", "pk_test_something"):
        monkeypatch.setattr(settings, "stripe_secret_key", key)
        response = await new_topup(client, rider)
        assert response.status_code == 503 and response.json()["detail"] == "Card payments are not configured"
        assert await count(db, WalletTopup) == 0
    assert "sk_live_unit_test_secret_value" not in logged.text and "pk_test_something" not in logged.text


async def test_creating_a_topup_sends_stripe_exactly_what_it_needs(client, db, stripe_configured, rider):
    response = await new_topup(client, rider, 25000)
    assert response.status_code == 201
    topup = response.json()
    assert set(topup) == {"id", "amount", "status", "checkout_url", "created_at", "completed_at"}
    assert (topup["amount"], topup["status"], topup["completed_at"]) == (25000, "PENDING", None)
    assert topup["checkout_url"].startswith("https://checkout.test/cs_test_topup-")

    assert len(stripe_configured.calls) == 1
    call = stripe_configured.calls[0]
    form = call["form"]
    assert (call["method"], call["path"], call["key"]) == ("POST", "/v1/checkout/sessions", f"topup-{topup['id']}")
    assert form["mode"] == "payment"
    assert form["line_items[0][price_data][currency]"] == "inr"
    assert form["line_items[0][price_data][unit_amount]"] == 25000
    assert form["line_items[0][quantity]"] == 1
    assert str(form["client_reference_id"]) == str(topup["id"])
    assert (str(form["metadata[topup_id]"]), str(form["metadata[user_id]"])) == (str(topup["id"]), str(rider["user"].id))
    assert form["success_url"] == f"http://app.test/rider/?topup=success&id={topup['id']}"
    assert form["cancel_url"] == "http://app.test/rider/?topup=cancelled"
    assert time.time() + 3500 < form["expires_at"] < time.time() + 3700  # about an hour: Stripe takes 30 minutes to 24 hours
    stored = await topup_state(db, topup["id"])
    assert stored.stripe_session_id == "cs_test_" + call["key"]

    listed = (await client.get("/wallet/topups", headers=rider["headers"])).json()
    assert [t["id"] for t in listed] == [topup["id"]]
    assert (await client.get("/wallet/topups?limit=0", headers=rider["headers"])).status_code == 422
    assert (await client.get("/wallet/topups?limit=51", headers=rider["headers"])).status_code == 422


async def test_topup_validation_and_roles(client, db, stripe_configured, rider, driver, admin):
    for amount in (9999, 1000001, 0, -100, 100.5, "30000", None, True):
        assert (await new_topup(client, rider, amount)).status_code == 422, amount
    assert (await new_topup(client, rider, 9999)).json()["detail"] == "Amount must be between ₹100 and ₹10,000"
    for key in ("short", "has spaces in it", "x" * 65, "bad/key/chars"):
        response = await client.post("/wallet/topups", json={"amount": 30000}, headers={**rider["headers"], "Idempotency-Key": key})
        assert response.status_code == 422, key
    assert (await client.post("/wallet/topups", json={"amount": 30000}, headers=rider["headers"])).status_code == 422
    assert (await new_topup(client, rider, 10000)).status_code == 201
    assert (await new_topup(client, rider, 1000000)).status_code == 201
    assert await count(db, WalletTopup) == 2 and len(stripe_configured.calls) == 2

    assert (await new_topup(client, driver)).status_code == 403
    assert (await new_topup(client, admin)).status_code == 403
    assert (await client.post("/wallet/topups", json={"amount": 30000}, headers={"Idempotency-Key": "key-no-token-1"})).status_code == 401
    assert (await client.post("/wallet/topups/1/sync", headers=driver["headers"])).status_code == 403


async def test_replays_and_failures_of_topup_creation(client, db, stripe_configured, rider):
    first = await new_topup(client, rider, 30000, "replay-key-1")
    again = await new_topup(client, rider, 30000, "replay-key-1")
    assert (first.status_code, again.status_code) == (201, 200)
    assert again.json() == first.json() and len(stripe_configured.calls) == 1
    different = await new_topup(client, rider, 40000, "replay-key-1")
    assert different.status_code == 409 and different.json()["detail"] == "This idempotency key was already used with a different request"

    # Stripe fails between "row stored" and "session stored": a retry with the same key finishes the job.
    stripe_configured.fail = "timeout"
    failed = await new_topup(client, rider, 30000, "retry-key-1")
    assert failed.status_code == 502 and failed.json()["detail"] == "Card payments are unavailable"
    row = (await db.execute(select(WalletTopup.id, WalletTopup.status, WalletTopup.stripe_session_id, WalletTopup.checkout_url)
                            .where(WalletTopup.idempotency_key == "retry-key-1"))).one()
    assert (row.status, row.stripe_session_id, row.checkout_url) == (TopupStatus.PENDING, None, None)
    stripe_configured.fail = None
    retried = await new_topup(client, rider, 30000, "retry-key-1")
    assert retried.status_code == 200 and retried.json()["id"] == row.id and retried.json()["checkout_url"]
    assert [c["key"] for c in stripe_configured.calls[-2:]] == [f"topup-{row.id}"] * 2  # the same Stripe key both times
    assert await count(db, WalletTopup, WalletTopup.idempotency_key == "retry-key-1") == 1

    stripe_configured.fail = "409"
    busy = await new_topup(client, rider, 30000, "busy-key-1")
    assert busy.status_code == 409 and "in progress" in busy.json()["detail"]
    stripe_configured.fail = None
    assert (await new_topup(client, rider, 30000, "busy-key-1")).status_code == 200  # the row existed: not "created" again


async def test_ten_simultaneous_creations_with_one_key_make_one_topup_and_one_session(client, db, stripe_configured, rider):
    stripe_configured.delay = 0.05
    answers = await asyncio.gather(*[new_topup(client, rider, 30000, "burst-key-1") for _ in range(10)])
    assert {a.status_code for a in answers} <= {200, 201} and [a.status_code for a in answers].count(201) == 1
    assert len({a.json()["id"] for a in answers}) == 1
    assert await count(db, WalletTopup) == 1
    assert len({call["key"] for call in stripe_configured.calls}) == 1
    assert len(stripe_sessions := stripe_configured.sessions) == 1 and stripe_sessions


def mock_stripe(monkeypatch, handler):
    """Makes call_stripe's httpx client talk to `handler` instead of the network. Returns the requests it saw."""
    seen = []

    def respond(request):
        seen.append(request)
        return handler(request)

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: REAL_ASYNC_CLIENT(transport=httpx.MockTransport(respond), **kwargs))
    return seen


async def test_call_stripe_builds_the_request_and_maps_the_answers(monkeypatch, logged):
    monkeypatch.setattr(settings, "stripe_secret_key", SECRET_KEY)
    monkeypatch.setattr(settings, "stripe_api_url", "http://stripe.test/")
    seen = mock_stripe(monkeypatch, lambda request: httpx.Response(200, json={"id": "cs_test_1", "url": "https://x.test"}))

    form = {"mode": "payment", "line_items[0][price_data][unit_amount]": 30000, "metadata[topup_id]": 5}
    assert await payments_service.call_stripe("POST", "/v1/checkout/sessions", form, "topup-5") == {"id": "cs_test_1", "url": "https://x.test"}
    request = seen[0]
    assert (request.method, str(request.url)) == ("POST", "http://stripe.test/v1/checkout/sessions")
    assert request.headers["authorization"] == "Basic " + base64.b64encode(f"{SECRET_KEY}:".encode()).decode()
    assert request.headers["idempotency-key"] == "topup-5" and "stripe-version" not in request.headers
    assert request.headers["content-type"] == "application/x-www-form-urlencoded"
    assert parse_qs(request.content.decode()) == {
        "mode": ["payment"], "line_items[0][price_data][unit_amount]": ["30000"], "metadata[topup_id]": ["5"]
    }
    assert b"%5B" in request.content  # the brackets are percent-encoded
    assert request.extensions["timeout"]["read"] == payments_service.STRIPE_TIMEOUT_S

    await payments_service.call_stripe("GET", "/v1/checkout/sessions/cs_test_1")
    assert "idempotency-key" not in seen[1].headers and seen[1].content == b""

    async def status_of(handler):
        mock_stripe(monkeypatch, handler)
        with pytest.raises(HTTPException) as error:
            await payments_service.call_stripe("POST", "/v1/checkout/sessions", {}, "topup-6")
        return error.value.status_code, error.value.detail

    def raise_timeout(request):
        raise httpx.ReadTimeout("slow", request=request)

    unavailable = (502, "Card payments are unavailable")
    assert await status_of(lambda r: httpx.Response(500, json={"error": {"code": "api_error", "message": "secret details"}})) == unavailable
    assert await status_of(lambda r: httpx.Response(400, json={"error": {"code": "parameter_invalid_integer"}})) == unavailable
    assert await status_of(lambda r: httpx.Response(200, content=b"<html>not json</html>")) == unavailable
    assert await status_of(lambda r: httpx.Response(200, json=["not", "an", "object"])) == unavailable
    assert await status_of(raise_timeout) == unavailable
    assert await status_of(lambda r: httpx.Response(409, json={"error": {"code": "lock_timeout"}})) == (
        409, "Another request with this idempotency key is in progress. Retry in a moment."
    )
    assert "api_error" in logged.text and "secret details" not in logged.text  # status and code only, never the body
    assert SECRET_KEY not in logged.text and "topup-6" not in logged.text

    monkeypatch.setattr(settings, "stripe_secret_key", "sk_live_nope")
    with pytest.raises(HTTPException) as error:
        await payments_service.call_stripe("GET", "/v1/anything")
    assert error.value.status_code == 503


# --- D. the webhook ---


async def test_signature_checks(client, db, stripe_configured, monkeypatch):
    body = event_body("ping", {"id": "x"})
    now = int(time.time())

    assert (await post_webhook(client, body)).json() == {"status": "ignored"}
    assert await count(db, StripeEvent) == 1

    bad = {
        "wrong secret": lambda b: sign(b, "whsec_someone_else"),
        "missing header": lambda b: None,
        "malformed header": lambda b: "garbage",
        "no signature": lambda b: f"t={now}",
        "no timestamp": lambda b: "v1=" + "0" * 64,
        "non-numeric timestamp": lambda b: "t=abc,v1=" + "0" * 64,
        "six minutes old": lambda b: sign(b, timestamp=now - 360),
        "six minutes ahead": lambda b: sign(b, timestamp=now + 360),
        "only v0": lambda b: f"t={now},v0=" + "0" * 64,
        "non-ascii signature": lambda b: f"t={now},v1=ééé".encode(),
    }
    for name, make_header in bad.items():
        fresh = event_body("ping", {"id": "x"})
        headers = {"Content-Type": "application/json"}
        if make_header(fresh) is not None:
            headers["Stripe-Signature"] = make_header(fresh)
        response = await client.post("/webhooks/stripe", content=fresh, headers=headers)
        assert response.status_code == 400 and response.json()["detail"] == "Invalid signature", name
    tampered = event_body("ping", {"id": "x"})
    response = await client.post("/webhooks/stripe", content=tampered + b" ", headers={"Stripe-Signature": sign(tampered)})
    assert response.status_code == 400
    assert await count(db, StripeEvent) == 1  # nothing was recorded for the refused ones

    several = event_body("ping", {"id": "x"})
    header = sign(several)
    timestamp, good = header.split(",")
    accepted = await client.post("/webhooks/stripe", content=several, headers={"Stripe-Signature": f"{timestamp},v1={'0' * 64},{good},v0=abc"})
    assert accepted.status_code == 200

    monkeypatch.setattr(settings, "stripe_webhook_secret", "")
    assert (await post_webhook(client, event_body("ping", {"id": "x"}))).status_code == 503
    monkeypatch.setattr(settings, "stripe_webhook_secret", WEBHOOK_SECRET)
    huge = b"x" * (payments_service.MAX_WEBHOOK_BYTES + 1)
    assert (await client.post("/webhooks/stripe", content=huge, headers={"Stripe-Signature": sign(huge)})).status_code == 413
    not_json = b"this is not json"
    assert (await post_webhook(client, not_json)).status_code == 400
    assert (await post_webhook(client, json.dumps({"no": "id"}).encode())).status_code == 400
    assert await count(db, StripeEvent) == 2


async def test_a_paid_checkout_completed_event_credits_the_wallet_once(client, db, stripe_configured, rider):
    topup_id, session = await paid_topup(client, db, stripe_configured, rider, 30000)
    response = await post_webhook(client, event_body("checkout.session.completed", session, "evt_one"))
    assert response.status_code == 200 and response.json() == {"status": "processed"}

    stored = await topup_state(db, topup_id)
    assert (stored.status, stored.stripe_payment_intent_id) == (TopupStatus.SUCCEEDED, f"pi_test_{topup_id}")
    assert stored.completed_at is not None
    entries = await entries_of(db, rider["user"].id)
    assert [(e.amount, e.kind, e.topup_id, e.balance_after, e.ride_id) for e in entries] == [(30000, WalletEntryKind.TOPUP, topup_id, 30000, None)]
    assert await balance_of(db, rider["user"].id) == 30000
    assert [e for e in (await db.execute(select(StripeEvent.id, StripeEvent.event_type))).all()] == [("evt_one", "checkout.session.completed")]
    done = (await client.get("/wallet/topups", headers=rider["headers"])).json()[0]
    assert done["status"] == "SUCCEEDED" and done["checkout_url"] is None and done["completed_at"] is not None
    assert (await get_wallet(client, rider))["balance"] == 30000


async def test_replayed_and_duplicate_events_credit_once(client, db, stripe_configured, rider):
    topup_id, session = await paid_topup(client, db, stripe_configured, rider)
    body = event_body("checkout.session.completed", session, "evt_same")
    header = sign(body)

    async def send():
        return await client.post("/webhooks/stripe", content=body, headers={"Stripe-Signature": header})

    answers = await asyncio.gather(*[send() for _ in range(10)])
    assert [a.status_code for a in answers] == [200] * 10
    results = sorted(a.json()["status"] for a in answers)
    assert results == ["duplicate"] * 9 + ["processed"]
    assert [a.json()["status"] for a in await asyncio.gather(*[send() for _ in range(3)])] == ["duplicate"] * 3

    other_event = await post_webhook(client, event_body("checkout.session.completed", session, "evt_other"))
    assert other_event.status_code == 200 and other_event.json() == {"status": "ignored"}
    assert await count(db, WalletEntry) == 1 and await balance_of(db, rider["user"].id) == 30000
    assert await count(db, StripeEvent) == 2


@pytest.mark.parametrize(
    "name, change",
    [
        ("amount", lambda s: {"amount_total": s["amount_total"] + 100}),
        ("currency", lambda s: {"currency": "usd"}),
        ("session", lambda s: {"id": "cs_test_somebody_else"}),
        ("unpaid", lambda s: {"payment_status": "unpaid"}),
        ("unknown reference", lambda s: {"client_reference_id": "999999"}),
        ("text reference", lambda s: {"client_reference_id": "abc"}),
        ("huge reference", lambda s: {"client_reference_id": "9" * 30}),
        ("no reference", lambda s: {"client_reference_id": None}),
    ],
)
async def test_an_event_that_does_not_match_our_row_is_recorded_but_never_credited(
    client, db, stripe_configured, rider, logged, name, change
):
    topup_id, session = await paid_topup(client, db, stripe_configured, rider)
    session.update(change(session))
    response = await post_webhook(client, event_body("checkout.session.completed", session))
    assert response.status_code == 200 and response.json() == {"status": "ignored"}
    assert await count(db, WalletEntry) == 0 and await balance_of(db, rider["user"].id) == 0
    assert (await topup_state(db, topup_id)).status == TopupStatus.PENDING
    assert await count(db, StripeEvent) == 1
    if name in ("amount", "currency", "session"):
        assert any(r.levelno >= logging.ERROR and "not credited" in r.getMessage() for r in logged.records)
    if name == "unknown reference":
        assert any(r.levelno >= logging.WARNING and "unknown top-up" in r.getMessage() for r in logged.records)


async def test_a_session_of_another_riders_topup_cannot_credit_this_one(client, db, stripe_configured, rider, make_user):
    other = await make_user("rider")
    mine, _ = await paid_topup(client, db, stripe_configured, rider)
    theirs, their_session = await paid_topup(client, db, stripe_configured, other)
    their_session["client_reference_id"] = str(mine)  # the metadata points at my top-up, the money is another session's
    response = await post_webhook(client, event_body("checkout.session.completed", their_session))
    assert response.json() == {"status": "ignored"}
    assert await count(db, WalletEntry) == 0
    assert (await topup_state(db, mine)).status == (await topup_state(db, theirs)).status == TopupStatus.PENDING


async def test_expired_events_and_unknown_event_types(client, db, stripe_configured, rider):
    topup_id, paid = await paid_topup(client, db, stripe_configured, rider)
    session = {**paid, "payment_status": "unpaid", "status": "expired"}
    assert (await post_webhook(client, event_body("checkout.session.expired", session))).json() == {"status": "processed"}
    assert (await topup_state(db, topup_id)).status == TopupStatus.EXPIRED
    assert (await client.get("/wallet/topups", headers=rider["headers"])).json()[0]["checkout_url"] is None

    # The money was taken after all (a late payment): an expired top-up is credited.
    assert (await post_webhook(client, event_body("checkout.session.completed", paid))).json() == {"status": "processed"}
    assert (await topup_state(db, topup_id)).status == TopupStatus.SUCCEEDED
    assert await balance_of(db, rider["user"].id) == 30000

    # Expired after SUCCEEDED changes nothing.
    assert (await post_webhook(client, event_body("checkout.session.expired", session))).json() == {"status": "ignored"}
    assert (await topup_state(db, topup_id)).status == TopupStatus.SUCCEEDED

    unknown = event_body("customer.created", {"id": "cus_1"}, "evt_unknown")
    assert (await post_webhook(client, unknown)).json() == {"status": "ignored"}
    assert await count(db, StripeEvent, StripeEvent.id == "evt_unknown") == 1
    async_paid = event_body("checkout.session.async_payment_succeeded", paid)
    assert (await post_webhook(client, async_paid)).json() == {"status": "ignored"}  # already credited


async def test_a_failure_in_the_middle_leaves_nothing_behind_and_the_retry_credits(client, db, stripe_configured, rider, monkeypatch):
    topup_id, session = await paid_topup(client, db, stripe_configured, rider)
    body = event_body("checkout.session.completed", session, "evt_retry")
    real = wallet_service.post_entry

    async def broken(*args, **kwargs):
        raise RuntimeError("the ledger is broken")

    monkeypatch.setattr(wallet_service, "post_entry", broken)
    assert (await post_webhook(client, body)).status_code == 500
    assert await count(db, StripeEvent) == 0  # the event id was rolled back with everything else
    assert (await topup_state(db, topup_id)).status == TopupStatus.PENDING and await count(db, WalletEntry) == 0

    monkeypatch.setattr(wallet_service, "post_entry", real)
    assert (await post_webhook(client, body)).json() == {"status": "processed"}
    assert await count(db, WalletEntry) == 1 and await balance_of(db, rider["user"].id) == 30000


@pytest.mark.parametrize("round_number", range(10))
async def test_the_webhook_and_sync_racing_credit_once(client, db, stripe_configured, rider, round_number):
    topup_id, session = await paid_topup(client, db, stripe_configured, rider)
    hook = post_webhook(client, event_body("checkout.session.completed", session))
    sync = client.post(f"/wallet/topups/{topup_id}/sync", headers=rider["headers"])
    answers = await asyncio.gather(*((hook, sync) if round_number % 2 == 0 else (sync, hook)))
    assert all(a.status_code == 200 for a in answers)
    assert await count(db, WalletEntry, WalletEntry.topup_id == topup_id) == 1
    assert await balance_of(db, rider["user"].id) == 30000
    assert (await topup_state(db, topup_id)).status == TopupStatus.SUCCEEDED


@pytest.mark.parametrize("round_number", range(10))
async def test_two_different_events_for_one_session_credit_once(client, db, stripe_configured, rider, round_number):
    topup_id, session = await paid_topup(client, db, stripe_configured, rider)
    first = post_webhook(client, event_body("checkout.session.completed", session))
    second = post_webhook(client, event_body("checkout.session.async_payment_succeeded", session))
    answers = await asyncio.gather(*((first, second) if round_number % 2 == 0 else (second, first)))
    assert sorted(a.json()["status"] for a in answers) == ["ignored", "processed"]
    assert await count(db, WalletEntry, WalletEntry.topup_id == topup_id) == 1
    assert await count(db, StripeEvent) == 2


async def test_no_secret_or_key_ever_reaches_the_log(client, db, stripe_configured, rider, monkeypatch, logged):
    logged.set_level(logging.DEBUG)
    logged.set_level(logging.DEBUG, logger="uvicorn.error")
    key = "private-idempotency-key-123"
    topup_id = (await new_topup(client, rider, 30000, key)).json()["id"]
    session = stripe_configured.sessions[(await topup_state(db, topup_id)).stripe_session_id]
    session.update(payment_status="paid", status="complete")
    body = event_body("checkout.session.completed", dict(session))
    header = sign(body)
    await client.post("/webhooks/stripe", content=body, headers={"Stripe-Signature": header})
    await client.post("/webhooks/stripe", content=body, headers={"Stripe-Signature": "t=1,v1=" + "a" * 64})
    await client.post("/webhooks/stripe", content=body, headers={"Stripe-Signature": header})
    await client.post(f"/wallet/topups/{topup_id}/sync", headers=rider["headers"])

    # The real call_stripe on a failing answer, with a body that must not be logged.
    mock_stripe(monkeypatch, lambda request: httpx.Response(500, json={"error": {"code": "api_error", "message": "private body text"}}))
    with pytest.raises(HTTPException):
        await real_call_stripe("POST", "/v1/checkout/sessions", {}, "another-private-key-9")

    everything = "\n".join(record.getMessage() for record in logged.records)
    assert "api_error" in everything and "processed" in everything
    for secret in (SECRET_KEY, WEBHOOK_SECRET, header, header.split(",")[1], key, "another-private-key-9", body.decode(), "private body text"):
        assert secret not in everything


# --- E. sync ---


async def test_sync_applies_what_stripe_says_without_a_webhook(client, db, stripe_configured, rider, make_user):
    topup_id, session = await paid_topup(client, db, stripe_configured, rider)
    done = await client.post(f"/wallet/topups/{topup_id}/sync", headers=rider["headers"])
    assert done.status_code == 200 and done.json()["status"] == "SUCCEEDED" and done.json()["checkout_url"] is None
    assert await balance_of(db, rider["user"].id) == 30000 and await count(db, WalletEntry) == 1
    calls = len(stripe_configured.calls)
    again = await client.post(f"/wallet/topups/{topup_id}/sync", headers=rider["headers"])
    assert again.status_code == 200 and again.json()["status"] == "SUCCEEDED" and len(stripe_configured.calls) == calls
    assert await count(db, WalletEntry) == 1

    open_id = (await new_topup(client, rider)).json()["id"]
    still_open = await client.post(f"/wallet/topups/{open_id}/sync", headers=rider["headers"])
    assert still_open.json()["status"] == "PENDING" and still_open.json()["checkout_url"]

    stripe_configured.sessions[(await topup_state(db, open_id)).stripe_session_id].update(status="expired")
    assert (await client.post(f"/wallet/topups/{open_id}/sync", headers=rider["headers"])).json()["status"] == "EXPIRED"
    assert await balance_of(db, rider["user"].id) == 30000

    other = await make_user("rider")
    assert (await client.post(f"/wallet/topups/{open_id}/sync", headers=other["headers"])).status_code == 404
    assert (await client.post("/wallet/topups/99999/sync", headers=rider["headers"])).status_code == 404

    pending_id = (await new_topup(client, rider)).json()["id"]
    stripe_configured.fail = "timeout"
    assert (await client.post(f"/wallet/topups/{pending_id}/sync", headers=rider["headers"])).status_code == 502
    stripe_configured.fail = None

    # No session yet (Stripe failed during creation): sync returns the row as it is and asks Stripe nothing.
    stripe_configured.fail = "timeout"
    assert (await new_topup(client, rider, 30000, "no-session-key-1")).status_code == 502
    stripe_configured.fail = None
    calls = len(stripe_configured.calls)
    no_session = (await db.execute(select(WalletTopup.id).where(WalletTopup.idempotency_key == "no-session-key-1"))).scalar_one()
    assert (await client.post(f"/wallet/topups/{no_session}/sync", headers=rider["headers"])).json()["status"] == "PENDING"
    assert len(stripe_configured.calls) == calls
