from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app import database
from app.models import User, UserRole


async def get_by_email(db: AsyncSession, email: str) -> User | None:
    result = await db.execute(select(User).where(User.email == email))
    return result.scalar_one_or_none()


async def get_by_phone(db: AsyncSession, phone: str) -> User | None:
    result = await db.execute(select(User).where(User.phone == phone))
    return result.scalar_one_or_none()


async def get_by_id(db: AsyncSession, user_id: int) -> User | None:
    return await db.get(User, user_id)


async def create(
    db: AsyncSession, role: UserRole, name: str, email: str, phone: str | None, password_hash: str
) -> User:
    user = User(role=role, name=name, email=email, phone=phone, password_hash=password_hash)
    db.add(user)
    await db.flush()
    return user


async def lock(db: AsyncSession, user_id: int) -> None:
    """Blocks until this user's row is ours (FOR UPDATE), at most LOCK_WAIT_MS; the lock lasts until commit or rollback."""
    # SET cannot take a bind parameter; int() makes the value a plain number.
    await db.execute(text(f"SET LOCAL lock_timeout = {int(database.LOCK_WAIT_MS)}"))
    # Column only, never the entity: the identity map must not hand back an older copy.
    await db.execute(select(User.id).where(User.id == user_id).with_for_update())
