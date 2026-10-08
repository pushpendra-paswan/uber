import uuid

import httpx
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.config import settings
from app.database import Base, get_db
from app.main import app
from app.models import Driver, User, UserRole, Vehicle, VerificationStatus
from app.security import create_access_token

# Tests never touch the dev database: they use a copy of its name with a _test suffix.
TEST_DB_URL = make_url(settings.postgres_url).set(database=settings.postgres_db + "_test")
assert TEST_DB_URL.database.endswith("_test"), "Refusing to run tests against a database that is not named *_test"

ALL_TABLES = ", ".join(table.name for table in Base.metadata.sorted_tables)


@pytest_asyncio.fixture(scope="session")
async def test_engine():
    # Connect to the dev database only to create the test database if it is missing.
    admin_engine = create_async_engine(settings.postgres_url, isolation_level="AUTOCOMMIT", poolclass=NullPool)
    async with admin_engine.connect() as connection:
        exists = await connection.scalar(
            text("SELECT 1 FROM pg_database WHERE datname = :name"), {"name": TEST_DB_URL.database}
        )
        if not exists:
            await connection.execute(text(f'CREATE DATABASE "{TEST_DB_URL.database}"'))
    await admin_engine.dispose()

    # NullPool: every session opens its own connection, so simultaneous requests really run in parallel.
    engine = create_async_engine(TEST_DB_URL, poolclass=NullPool)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)
        await connection.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture(autouse=True)
async def clean_tables(test_engine):
    async with test_engine.begin() as connection:
        await connection.execute(text(f"TRUNCATE {ALL_TABLES} RESTART IDENTITY CASCADE"))


@pytest_asyncio.fixture
async def db(test_engine):
    async with async_sessionmaker(test_engine, expire_on_commit=False)() as session:
        yield session


@pytest_asyncio.fixture
async def client(test_engine):
    test_session = async_sessionmaker(test_engine, expire_on_commit=False)

    async def override_get_db():
        async with test_session() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as test_client:
        yield test_client
    app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def make_user(db: AsyncSession):
    """Returns a function that inserts a user (and, for drivers, a profile and vehicle) and its auth headers."""

    async def create(role: str, approved: bool = True) -> dict:
        unique = uuid.uuid4().hex[:8]
        user = User(role=UserRole(role), name=f"Test {role}", email=f"{role}-{unique}@example.com", password_hash="unused")
        db.add(user)
        await db.flush()

        driver = None
        if role == "driver":
            status = VerificationStatus.approved if approved else VerificationStatus.pending
            driver = Driver(user_id=user.id, license_number=f"LIC-{unique}", verification_status=status)
            db.add(driver)
            await db.flush()
            db.add(Vehicle(driver_id=driver.id, plate_number=f"PLATE{unique}".upper(), model="Swift", color="white"))
        await db.commit()

        headers = {"Authorization": f"Bearer {create_access_token(user)}"}
        return {"user": user, "driver": driver, "headers": headers}

    return create


@pytest_asyncio.fixture
async def rider(make_user):
    return await make_user("rider")


@pytest_asyncio.fixture
async def driver(make_user):
    return await make_user("driver")


@pytest_asyncio.fixture
async def admin(make_user):
    return await make_user("admin")
