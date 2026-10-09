from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Wallet, WalletEntry, WalletEntryKind


async def lock_wallet(db: AsyncSession, user_id: int) -> int:
    """Creates the user's wallet row if there is none, locks it (FOR UPDATE) until commit or rollback, and returns the
    balance. Column only, never the entity, so the identity map cannot hand back an older copy."""
    await db.execute(insert(Wallet).values(user_id=user_id).on_conflict_do_nothing())
    result = await db.execute(select(Wallet.balance).where(Wallet.user_id == user_id).with_for_update())
    return result.scalar_one()


async def get_balance(db: AsyncSession, user_id: int) -> int:
    """No lock, and a user with no wallet row has balance 0 (reading never creates a row)."""
    result = await db.execute(select(Wallet.balance).where(Wallet.user_id == user_id))
    return result.scalar_one_or_none() or 0


async def insert_entry(
    db: AsyncSession, user_id: int, amount: int, kind: WalletEntryKind, balance_after: int, ride_id: int | None,
    topup_id: int | None, idempotency_key: str | None, note: str | None, actor_user_id: int | None,
) -> dict:
    result = await db.execute(
        insert(WalletEntry)
        .values(
            user_id=user_id, amount=amount, kind=kind, balance_after=balance_after, ride_id=ride_id, topup_id=topup_id,
            idempotency_key=idempotency_key, note=note, actor_user_id=actor_user_id,
        )
        .returning(WalletEntry.__table__)
    )
    return dict(result.mappings().one())


async def set_balance(db: AsyncSession, user_id: int, balance: int) -> None:
    await db.execute(update(Wallet).where(Wallet.user_id == user_id).values(balance=balance, updated_at=func.now()))


async def get_entry_by_key(db: AsyncSession, user_id: int, key: str) -> dict | None:
    result = await db.execute(
        select(WalletEntry.__table__).where(WalletEntry.user_id == user_id, WalletEntry.idempotency_key == key)
    )
    row = result.mappings().first()
    return dict(row) if row is not None else None


async def get_charge_entry(db: AsyncSession, ride_id: int) -> int | None:
    """The balance_after of the RIDE_CHARGE entry of a ride, or None when the ride was not paid from the wallet."""
    result = await db.execute(
        select(WalletEntry.balance_after).where(WalletEntry.ride_id == ride_id, WalletEntry.kind == WalletEntryKind.RIDE_CHARGE)
    )
    return result.scalar_one_or_none()


async def list_entries(db: AsyncSession, user_id: int, limit: int, before_id: int | None) -> list[dict]:
    query = select(WalletEntry.__table__).where(WalletEntry.user_id == user_id)
    if before_id is not None:
        query = query.where(WalletEntry.id < before_id)
    result = await db.execute(query.order_by(WalletEntry.id.desc()).limit(limit))
    return [dict(row) for row in result.mappings().all()]
