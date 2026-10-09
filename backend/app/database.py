from collections.abc import AsyncGenerator

from redis.asyncio import Redis
from sqlalchemy import MetaData
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.config import settings

# How long a request waits for a row lock held by someone else before answering 503 (repositories/users.py, drivers.py).
LOCK_WAIT_MS = 3000

# Predictable constraint names, so Alembic migrations can drop and alter them by name.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


engine = create_async_engine(settings.postgres_url)
async_session = async_sessionmaker(engine, expire_on_commit=False)
redis_client = Redis(host=settings.redis_host, port=settings.redis_port, db=settings.redis_db, decode_responses=True)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with async_session() as session:
        yield session
