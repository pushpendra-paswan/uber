import hashlib
import hmac
import json
import logging
from datetime import datetime, timezone

import httpx
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import PaymentMethod, Ride, TopupStatus, User, WalletEntryKind, WalletTopup
from app.repositories import earnings as earnings_repo
from app.repositories import payments as payments_repo
from app.repositories import pricing as pricing_repo
from app.schemas import TopupCreate
from app.services import wallet

TOPUP_MIN_PAISE = 10000  # 100 rupees
TOPUP_MAX_PAISE = 1000000  # 10,000 rupees
CURRENCY = "inr"
STRIPE_TIMEOUT_S = 10
CHECKOUT_EXPIRES_S = 3600  # Stripe accepts 30 minutes to 24 hours
WEBHOOK_TOLERANCE_S = 300
MAX_WEBHOOK_BYTES = 262144
TEST_KEY_PREFIXES = ("sk_test_", "rk_test_")  # a safety rule for a learning project: no other key is ever used
PAID_EVENT_TYPES = ("checkout.session.completed", "checkout.session.async_payment_succeeded")
NOT_CONFIGURED = "Card payments are not configured"
KEY_REUSED = "This idempotency key was already used with a different request"

# uvicorn's logger, because it is the one that has a handler and prints INFO.
# Never log a key, a secret, the signature header, an idempotency key or the raw body.
logger = logging.getLogger("uvicorn.error")


async def charge_ride(db: AsyncSession, ride: Ride) -> None:
    """Charges a ride that was just settled (COMPLETED or CANCELLED). Does not commit: it runs in the settlement's
    transaction, under the ride lock, so a settled ride always has its payment and its earning row. A fee of 0 creates nothing.
    No try/except: a duplicate (unique violation) would be a bug, and must surface and undo the whole settlement."""
    if not ride.final_fare:
        return
    payment_id = await payments_repo.create_payment(db, ride.id, ride.final_fare, ride.payment_method, f"ride:{ride.id}:charge")

    # The split of the payment between the platform and the driver, at the commission of this moment (a snapshot on the
    # row). Half up on the platform's part; the driver gets the rest, so rounding never creates or loses a paisa.
    rule = await pricing_repo.get_rule(db, "economy")
    if rule is None:
        raise HTTPException(status_code=503, detail="Pricing is not configured")
    platform_fee = (ride.final_fare * rule.commission_percent + 50) // 100
    await earnings_repo.create(
        db, ride.id, payment_id, ride.driver_id, ride.fare_breakdown["kind"], ride.final_fare, rule.commission_percent,
        platform_fee, ride.final_fare - platform_fee,
    )

    # Cash is assumed collected by the driver. Only a wallet ride moves money in the ledger.
    if ride.payment_method == PaymentMethod.wallet:
        await wallet.post_entry(db, ride.rider_id, -ride.final_fare, WalletEntryKind.RIDE_CHARGE, ride_id=ride.id)


async def call_stripe(method: str, path: str, form: dict | None = None, idempotency_key: str | None = None) -> dict:
    """The one place that talks to Stripe (httpx, form encoded, no SDK). Any failure becomes an HTTP error for our caller."""
    if not settings.stripe_secret_key.startswith(TEST_KEY_PREFIXES):
        raise HTTPException(status_code=503, detail=NOT_CONFIGURED)
    headers = {"Idempotency-Key": idempotency_key} if idempotency_key else {}
    try:
        async with httpx.AsyncClient(timeout=STRIPE_TIMEOUT_S) as client:
            response = await client.request(
                method, settings.stripe_api_url.rstrip("/") + path, data=form, auth=(settings.stripe_secret_key, ""), headers=headers
            )
        body = response.json() if response.content else None
    except (httpx.HTTPError, ValueError) as error:
        logger.warning("Stripe %s %s failed: %s", method, path, type(error).__name__)
        raise HTTPException(status_code=502, detail="Card payments are unavailable")

    error_code = body["error"].get("code") if isinstance(body, dict) and isinstance(body.get("error"), dict) else None
    if response.status_code == 409:
        logger.warning("Stripe %s %s answered 409 (%s)", method, path, error_code)
        raise HTTPException(status_code=409, detail="Another request with this idempotency key is in progress. Retry in a moment.")
    if not response.is_success or not isinstance(body, dict):
        logger.warning("Stripe %s %s answered %s (%s)", method, path, response.status_code, error_code)
        raise HTTPException(status_code=502, detail="Card payments are unavailable")
    return body


async def create_topup(db: AsyncSession, user: User, key: str, data: TopupCreate) -> tuple[WalletTopup, bool]:
    """Starts a top-up and returns it with whether this call created the row (False for a replay of the same key).

    Idempotent twice: our unique (user, key) returns the same row for the same key, and the Stripe call carries a key
    derived from OUR row, so a retry after a crash between "row stored" and "session stored" gets the same Checkout Session."""
    if not TOPUP_MIN_PAISE <= data.amount <= TOPUP_MAX_PAISE:
        raise HTTPException(status_code=422, detail="Amount must be between ₹100 and ₹10,000")
    if not settings.stripe_secret_key.startswith(TEST_KEY_PREFIXES):
        raise HTTPException(status_code=503, detail=NOT_CONFIGURED)

    topup = await payments_repo.insert_topup_if_new(db, user.id, key, data.amount)
    created = topup is not None
    if topup is None:
        topup = await payments_repo.get_topup_by_key(db, user.id, key)
        if topup.amount != data.amount:
            raise HTTPException(status_code=409, detail=KEY_REUSED)
    # Committed BEFORE the Stripe call: no lock is held across it, and a crash after it leaves a row a retry can finish.
    await db.commit()
    if topup.checkout_url is not None:
        return topup, created

    form = {
        "mode": "payment",
        "line_items[0][price_data][currency]": CURRENCY,
        "line_items[0][price_data][unit_amount]": topup.amount,
        "line_items[0][price_data][product_data][name]": "Wallet top-up",
        "line_items[0][quantity]": 1,
        "client_reference_id": topup.id,
        "metadata[topup_id]": topup.id,
        "metadata[user_id]": user.id,
        "success_url": f"{settings.app_base_url}/rider/?topup=success&id={topup.id}",
        "cancel_url": f"{settings.app_base_url}/rider/?topup=cancelled",
        # From the row's creation time, not from now: Stripe refuses a retry with the same Idempotency-Key whose
        # parameters differ, so a retry after a crash must send exactly the same form.
        "expires_at": int(topup.created_at.timestamp()) + CHECKOUT_EXPIRES_S,
    }
    session = await call_stripe("POST", "/v1/checkout/sessions", form, f"topup-{topup.id}")
    await payments_repo.set_topup_session(db, topup.id, session["id"], session["url"])
    await db.commit()
    return topup, created


async def list_topups(db: AsyncSession, user: User, limit: int) -> list[WalletTopup]:
    if not 1 <= limit <= 50:
        raise HTTPException(status_code=422, detail="limit must be between 1 and 50")
    return await payments_repo.list_topups(db, user.id, limit)


async def credit_topup(db: AsyncSession, topup_id: int, session: dict) -> str:
    """Credits a top-up once, whoever asks (the webhook or sync). `session` is a Stripe Checkout Session. Returns
    "processed" or "ignored". Does not commit: the caller's transaction holds the top-up lock until then."""
    # Lock, then check as a separate statement, then write. A second caller waits here and then finds SUCCEEDED.
    if await payments_repo.lock_topup(db, topup_id) is None:
        logger.warning("Stripe session for unknown top-up %s ignored", topup_id)
        return "ignored"
    topup = await payments_repo.get_topup(db, topup_id)
    if topup.status == TopupStatus.SUCCEEDED:
        return "ignored"
    if session.get("payment_status") != "paid":
        return "ignored"

    # Trust only our own row, never the metadata: the session must be the one we created, for the amount we asked.
    differences = [
        name for name, same in (
            ("session id", session.get("id") == topup.stripe_session_id),
            ("amount", session.get("amount_total") == topup.amount),
            ("currency", session.get("currency") == CURRENCY),
        ) if not same
    ]
    if differences:
        logger.error("Top-up %s not credited: the paid session differs in %s", topup.id, ", ".join(differences))
        return "ignored"

    await wallet.post_entry(db, topup.user_id, topup.amount, WalletEntryKind.TOPUP, topup_id=topup.id)
    await payments_repo.mark_topup_succeeded(db, topup.id, session.get("payment_intent"))
    return "processed"


async def sync_topup(db: AsyncSession, user: User, topup_id: int) -> WalletTopup:
    """Asks Stripe what became of a top-up and applies it. For when the webhook is late or missing (development)."""
    topup = await payments_repo.get_topup(db, topup_id)
    if topup is None or topup.user_id != user.id:
        raise HTTPException(status_code=404, detail="Top-up not found")
    if topup.status != TopupStatus.PENDING or topup.stripe_session_id is None:
        return topup

    session = await call_stripe("GET", f"/v1/checkout/sessions/{topup.stripe_session_id}")
    if session.get("payment_status") == "paid":
        await credit_topup(db, topup.id, session)
    elif session.get("status") == "expired":
        await payments_repo.mark_topup_expired(db, topup.id)
    await db.commit()
    return await payments_repo.get_topup(db, topup.id)


async def process_webhook(db: AsyncSession, raw_body: bytes, signature_header: str | None) -> dict:
    """Verifies and applies one Stripe webhook. Raises HTTPException for a request that must be refused; any other
    exception propagates (500, nothing committed), and Stripe retries the event."""
    secret = settings.stripe_webhook_secret
    if not secret:
        raise HTTPException(status_code=503, detail="Webhooks are not configured")
    if len(raw_body) > MAX_WEBHOOK_BYTES:
        raise HTTPException(status_code=413, detail="Request body too large")

    # Header: t=<unix time>,v1=<hex HMAC-SHA256 of "t.body">, possibly several v1 (a secret being rolled). Other schemes
    # are ignored. The signature is computed over the RAW bytes, exactly as received.
    reason = None
    if not signature_header:
        reason = "missing"
    else:
        parts = [item.split("=", 1) for item in signature_header.split(",") if "=" in item]
        timestamps = [value for name, value in parts if name.strip() == "t"]
        signatures = [value.strip() for name, value in parts if name.strip() == "v1"]
        if len(timestamps) != 1 or not timestamps[0].isascii() or not timestamps[0].isdigit() or not signatures:
            reason = "malformed"
        else:
            expected = hmac.new(secret.encode(), timestamps[0].encode() + b"." + raw_body, hashlib.sha256).hexdigest()
            if not any(hmac.compare_digest(expected.encode(), signature.encode()) for signature in signatures):
                reason = "no match"
            elif abs(datetime.now(timezone.utc).timestamp() - int(timestamps[0])) > WEBHOOK_TOLERANCE_S:
                reason = "too old"
    if reason is not None:
        logger.warning("Stripe webhook rejected: %s", reason)
        raise HTTPException(status_code=400, detail="Invalid signature")

    try:
        event = json.loads(raw_body)
        event_id, event_type, session = event["id"], event["type"], event["data"]["object"]
    except (ValueError, KeyError, TypeError):
        raise HTTPException(status_code=400, detail="Invalid payload")
    if not isinstance(event_id, str) or not isinstance(event_type, str) or not isinstance(session, dict):
        raise HTTPException(status_code=400, detail="Invalid payload")

    # The first statement: the event id, in the SAME transaction as the credit. A crash in the middle rolls the id back too,
    # so Stripe's retry is processed again instead of being thrown away as a duplicate.
    if not await payments_repo.insert_event_if_new(db, event_id, event_type):
        logger.info("Stripe event %s (%s): duplicate", event_id, event_type)
        return {"status": "duplicate"}

    reference = session.get("client_reference_id")
    # Our top-up ids are 32-bit integers: anything longer cannot be one of ours.
    topup_id = int(reference) if isinstance(reference, str) and reference.isascii() and reference.isdigit() and len(reference) < 10 else None
    result = "ignored"
    if event_type in PAID_EVENT_TYPES or event_type == "checkout.session.expired":
        if topup_id is None:
            logger.warning("Stripe event %s has no usable client_reference_id: ignored", event_id)
        elif event_type == "checkout.session.expired":
            result = "processed" if await payments_repo.mark_topup_expired(db, topup_id) else "ignored"
        else:
            result = await credit_topup(db, topup_id, session)
    await db.commit()
    logger.info("Stripe event %s (%s): %s", event_id, event_type, result)
    return {"status": result}
