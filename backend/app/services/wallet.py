from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import PaymentMethod, User, UserRole, WalletEntryKind
from app.repositories import rides as rides_repo
from app.repositories import users as users_repo
from app.repositories import wallet as wallet_repo
from app.schemas import AdjustRequest
from app.services import pricing

ADJUST_MAX_PAISE = 1000000  # 10,000 rupees
ENTRIES_DEFAULT_LIMIT = 20
ENTRIES_MAX_LIMIT = 100


# The ONLY function that writes wallets and wallet_entries. It never commits: the caller commits the entry together with
# the thing that caused it (the settled ride, the credited top-up, the adjustment), in one transaction.
#
# Lock order: ride row, then wallet row (settlement); top-up row, then wallet row (credit). The wallet row is a LEAF: while it
# is held nothing else is locked, and post_entry is the last database write before the commit in every flow. A leaf lock
# cannot be part of a cycle, so a wallet lock never deadlocks with the ride, offer, driver or top-up locks. Everyone who
# wants to change a balance waits here for the previous holder's commit, and because the entry is inserted after the lock,
# entry ids follow lock order (the running-sum invariant I14 relies on that).
async def post_entry(
    db: AsyncSession, user_id: int, amount: int, kind: WalletEntryKind, ride_id: int | None = None,
    topup_id: int | None = None, idempotency_key: str | None = None, note: str | None = None,
    actor_user_id: int | None = None,
) -> dict:
    balance = await wallet_repo.lock_wallet(db, user_id)
    new_balance = balance + amount
    entry = await wallet_repo.insert_entry(
        db, user_id, amount, kind, new_balance, ride_id, topup_id, idempotency_key, note, actor_user_id
    )
    await wallet_repo.set_balance(db, user_id, new_balance)
    return entry


async def get_wallet(db: AsyncSession, user: User) -> dict:
    balance = await wallet_repo.get_balance(db, user.id)
    # Derived, never stored: the rider has at most one active ride (unique index), so at most one amount is held back.
    ride = await rides_repo.get_active_for_rider(db, user.id)
    reserved = pricing.fare_cap(ride.fare_estimate) if ride is not None and ride.payment_method == PaymentMethod.wallet else 0
    return {"balance": balance, "reserved": reserved, "available": balance - reserved}


async def list_entries(db: AsyncSession, user: User, limit: int, before_id: int | None) -> list[dict]:
    if not 1 <= limit <= ENTRIES_MAX_LIMIT:
        raise HTTPException(status_code=422, detail=f"limit must be between 1 and {ENTRIES_MAX_LIMIT}")
    return await wallet_repo.list_entries(db, user.id, limit, before_id)


async def adjust(db: AsyncSession, admin: User, user_id: int, key: str, data: AdjustRequest) -> tuple[dict, bool]:
    """Credits or debits a rider's wallet by hand. Returns the entry and whether it is new (False for a replay)."""
    if data.amount == 0 or abs(data.amount) > ADJUST_MAX_PAISE:
        raise HTTPException(status_code=422, detail=f"Amount must not be 0 and at most ₹{ADJUST_MAX_PAISE // 100:,} either way")
    target = await users_repo.get_by_id(db, user_id)
    if target is None or target.role != UserRole.rider:
        raise HTTPException(status_code=404, detail="Rider not found")

    # Lock first, check the key second: two requests with one key take turns, and the second one sees the first one's entry.
    balance = await wallet_repo.lock_wallet(db, user_id)
    existing = await wallet_repo.get_entry_by_key(db, user_id, key)
    if existing is not None:
        if existing["kind"] != WalletEntryKind.ADJUSTMENT or existing["amount"] != data.amount or existing["note"] != data.note:
            raise HTTPException(status_code=409, detail="This idempotency key was already used with a different request")
        return existing, False
    if balance + data.amount < 0:
        raise HTTPException(status_code=409, detail="Adjustment would make the balance negative")

    entry = await post_entry(
        db, user_id, data.amount, WalletEntryKind.ADJUSTMENT, idempotency_key=key, note=data.note, actor_user_id=admin.id
    )
    await db.commit()
    return entry, True
