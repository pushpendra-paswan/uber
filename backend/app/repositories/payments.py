from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Payment, PaymentMethod, PaymentStatus, StripeEvent, TopupStatus, WalletTopup


async def create_payment(db: AsyncSession, ride_id: int, amount: int, method: PaymentMethod, key: str) -> int:
    payment = Payment(ride_id=ride_id, amount=amount, method=method, status=PaymentStatus.succeeded, idempotency_key=key)
    db.add(payment)
    await db.flush()
    return payment.id


async def get_payment_by_ride(db: AsyncSession, ride_id: int):
    """The payment of a ride as a row (method, amount, status, created_at), or None when the ride was not charged."""
    result = await db.execute(
        select(Payment.method, Payment.amount, Payment.status, Payment.created_at).where(Payment.ride_id == ride_id)
    )
    return result.first()


async def insert_topup_if_new(db: AsyncSession, user_id: int, key: str, amount: int) -> WalletTopup | None:
    """The new row, or None when this user already used this key (the unique constraint decides, atomically)."""
    result = await db.execute(
        insert(WalletTopup)
        .values(user_id=user_id, idempotency_key=key, amount=amount, status=TopupStatus.PENDING)
        .on_conflict_do_nothing()
        .returning(WalletTopup)
    )
    return result.scalar_one_or_none()


async def get_topup_by_key(db: AsyncSession, user_id: int, key: str) -> WalletTopup | None:
    result = await db.execute(select(WalletTopup).where(WalletTopup.user_id == user_id, WalletTopup.idempotency_key == key))
    return result.scalar_one_or_none()


async def get_topup(db: AsyncSession, topup_id: int) -> WalletTopup | None:
    # populate_existing: a copy loaded earlier in this session must not hide a change made since.
    result = await db.execute(select(WalletTopup).where(WalletTopup.id == topup_id).execution_options(populate_existing=True))
    return result.scalar_one_or_none()


async def lock_topup(db: AsyncSession, topup_id: int):
    """Locks the top-up row (FOR UPDATE) until commit or rollback. Column only; the row (id, status) or None."""
    result = await db.execute(select(WalletTopup.id, WalletTopup.status).where(WalletTopup.id == topup_id).with_for_update())
    return result.first()


async def set_topup_session(db: AsyncSession, topup_id: int, session_id: str, url: str) -> None:
    await db.execute(update(WalletTopup).where(WalletTopup.id == topup_id).values(stripe_session_id=session_id, checkout_url=url))


async def mark_topup_succeeded(db: AsyncSession, topup_id: int, payment_intent_id: str | None) -> None:
    await db.execute(
        update(WalletTopup)
        .where(WalletTopup.id == topup_id)
        .values(status=TopupStatus.SUCCEEDED, stripe_payment_intent_id=payment_intent_id, completed_at=func.now())
    )


async def mark_topup_expired(db: AsyncSession, topup_id: int) -> bool:
    """PENDING becomes EXPIRED; any other status is left alone. True when a row changed."""
    result = await db.execute(
        update(WalletTopup)
        .where(WalletTopup.id == topup_id, WalletTopup.status == TopupStatus.PENDING)
        .values(status=TopupStatus.EXPIRED)
    )
    return result.rowcount > 0


async def list_topups(db: AsyncSession, user_id: int, limit: int) -> list[WalletTopup]:
    result = await db.execute(select(WalletTopup).where(WalletTopup.user_id == user_id).order_by(WalletTopup.id.desc()).limit(limit))
    return list(result.scalars().all())


async def insert_event_if_new(db: AsyncSession, event_id: str, event_type: str) -> bool:
    """True when the Stripe event id was new. False means this event was already seen."""
    result = await db.execute(
        insert(StripeEvent).values(id=event_id, event_type=event_type).on_conflict_do_nothing().returning(StripeEvent.id)
    )
    return result.scalar_one_or_none() is not None
