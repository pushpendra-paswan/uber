import asyncio
import collections
import contextlib
import os
import time
import types
import uuid
from datetime import datetime, timedelta, timezone

import httpx
import jwt
import pytest
import pytest_asyncio
import uvicorn
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

# Before any app module is imported: tests use their own Redis database, so they never touch dev data.
TEST_REDIS_DB = 1
os.environ["REDIS_DB"] = str(TEST_REDIS_DB)

from app import database  # noqa: E402
from app.config import settings  # noqa: E402
from app.database import Base, get_db, redis_client  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Driver, PricingRule, Ride, RideEvent, RideStatus, User, UserRole, Vehicle, VerificationStatus  # noqa: E402
from app.repositories import drivers as drivers_repo  # noqa: E402
from app.repositories import users as users_repo  # noqa: E402
from app.routers import websocket as websocket_router  # noqa: E402
from app.security import create_access_token  # noqa: E402
from app.services import pricing, routing  # noqa: E402
from test_rides import RIDE_BODY  # noqa: E402

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
        # Same values as the seed migration (paise). TRUNCATE removed the migration's row, so put it back.
        await connection.execute(
            PricingRule.__table__.insert().values(
                vehicle_type="economy", base_fare=5000, per_km=1200, per_min=200, min_fare=8000, surge_cap=2.0,
                cancellation_fee=3000, free_cancel_seconds=120,
            )
        )


@pytest_asyncio.fixture(autouse=True)
async def clean_redis():
    # FLUSHDB wipes the whole connected database, so check that it is the test one first.
    assert settings.redis_db == TEST_REDIS_DB
    assert (await redis_client.client_info())["db"] == TEST_REDIS_DB, "Refusing to flush a Redis database that is not the test one"
    await redis_client.flushdb()


@pytest.fixture(autouse=True)
def use_test_session(test_engine, monkeypatch):
    """Code that opens its own session (WebSocket authentication, the offer sweeper) uses the _test database, not dev data."""
    monkeypatch.setattr(database, "async_session", async_sessionmaker(test_engine, expire_on_commit=False))


@pytest.fixture(autouse=True)
def fake_route(monkeypatch):
    """No test talks to a real OSRM. A test that needs another route replaces get_route again."""

    async def get_route(pickup_lat, pickup_lng, dropoff_lat, dropoff_lng):
        return {"distance_m": 5000, "duration_s": 900, "path": [[pickup_lat, pickup_lng], [dropoff_lat, dropoff_lng]]}

    monkeypatch.setattr(routing, "get_route", get_route)


@pytest.fixture
def fake_clock(monkeypatch):
    """Replaces the `time` module inside services/pricing.py (the trip meter's clock) with an object whose time() is
    controlled by the test: clock.now is the value, clock.advance(seconds) moves it."""
    clock = types.SimpleNamespace(now=1_000_000.0)
    clock.time = lambda: clock.now

    def advance(seconds: float) -> None:
        clock.now += seconds

    clock.advance = advance
    monkeypatch.setattr(pricing, "time", clock)
    return clock


def widen(monkeypatch, module, name: str, seconds: float, first_call_only: bool = False) -> None:
    """Makes a repository function sleep AFTER it has computed its result, so the answer is stale when the caller uses it.
    Services call repositories through the module (drivers_repo.get_available_ids), so patching the module attribute works."""
    real = getattr(module, name)
    calls = 0

    async def slow(*args, **kwargs):
        nonlocal calls
        result = await real(*args, **kwargs)
        calls += 1
        if not first_call_only or calls == 1:
            await asyncio.sleep(seconds)
        return result

    monkeypatch.setattr(module, name, slow)


@pytest.fixture
def locks_disabled(monkeypatch):
    """Turns the three row locks of M4.2 into nothing: they never block, never skip a driver, and never fail. What is left
    is the unique indexes of M4.3 (the safety net). Returns how often each lock function was called, so a test can check
    that the code under test really went through the place where the lock would be."""
    calls = collections.Counter()

    async def lock_user(db, user_id):
        calls["users.lock"] += 1

    async def lock_driver(db, driver_id):
        calls["drivers.lock"] += 1

    async def try_lock_driver(db, driver_id):
        calls["drivers.try_lock"] += 1
        return True

    monkeypatch.setattr(users_repo, "lock", lock_user)
    monkeypatch.setattr(drivers_repo, "lock", lock_driver)
    monkeypatch.setattr(drivers_repo, "try_lock", try_lock_driver)
    return calls


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


@pytest_asyncio.fixture
async def put_online(client):
    """Returns a function that puts a driver (from make_user or the driver fixture) online through the real API."""

    async def go_online(who: dict, lat: float, lng: float) -> None:
        response = await client.post("/drivers/me/online", json={"lat": lat, "lng": lng}, headers=who["headers"])
        assert response.status_code == 200

    return go_online


@pytest_asyncio.fixture
async def accept_offer(client):
    """Returns a function that has a driver accept their pending offer through the real API. Returns the ride."""

    async def accept(who: dict) -> dict:
        offer = await client.get("/drivers/me/offer", headers=who["headers"])
        assert offer.status_code == 200, offer.text
        accepted = await client.post(f"/offers/{offer.json()['id']}/accept", headers=who["headers"])
        assert accepted.status_code == 200, accepted.text
        return accepted.json()

    return accept


@pytest_asyncio.fixture
async def assign_ride(client, put_online, accept_offer):
    """Returns a function that makes a DRIVER_ASSIGNED ride: the driver goes online at the pickup, the rider requests,
    and the driver accepts the offer. Use it with a single driver, or the offer may go to a nearer one."""

    async def assign(rider: dict, driver: dict) -> dict:
        await put_online(driver, RIDE_BODY["pickup_lat"], RIDE_BODY["pickup_lng"])
        created = await client.post("/rides", json=RIDE_BODY, headers=rider["headers"])
        assert created.status_code == 201, created.text
        assert created.json()["status"] == "REQUESTED", created.text
        ride = await accept_offer(driver)
        assert ride["status"] == "DRIVER_ASSIGNED"
        assert ride["id"] == created.json()["id"]
        return ride

    return assign


@pytest_asyncio.fixture
async def insert_ride(db: AsyncSession):
    """Returns a function that inserts a ride straight into the database in the given status, with no matching.
    DRIVER_ASSIGNED and DRIVER_ARRIVED rides get the trip code 1234, unless with_otp is False. Every ride gets the numbers
    the fake route gives (5000 m, 900 s, fare 14000), because settlement uses them; a ride that was assigned has its
    DRIVER_ASSIGNED event, and an IN_PROGRESS ride has started_at."""

    async def create(rider: dict, status: RideStatus, driver: dict | None = None, with_otp: bool = True) -> Ride:
        # A ride that was assigned through the real flow has the trip code until the trip starts or is cancelled.
        has_otp = with_otp and status in (RideStatus.DRIVER_ASSIGNED, RideStatus.DRIVER_ARRIVED)
        ride = Ride(
            rider_id=rider["user"].id,
            driver_id=driver["driver"].id if driver else None,
            status=status,
            otp="1234" if has_otp else None,
            pickup_lat=settings.city_center_lat,
            pickup_lng=settings.city_center_lng,
            pickup_address="MG Road",
            dropoff_lat=(settings.city_center_lat + settings.city_south) / 2,
            dropoff_lng=(settings.city_center_lng + settings.city_east) / 2,
            dropoff_address="Koramangala",
            distance_m=5000,
            duration_s=900,
            fare_estimate=14000,
            started_at=datetime.now(timezone.utc) if status == RideStatus.IN_PROGRESS else None,
        )
        db.add(ride)
        await db.flush()
        if status in (RideStatus.DRIVER_ASSIGNED, RideStatus.DRIVER_ARRIVED, RideStatus.IN_PROGRESS):
            db.add(RideEvent(ride_id=ride.id, from_status=RideStatus.REQUESTED, to_status=RideStatus.DRIVER_ASSIGNED))
        await db.commit()
        return ride

    return create


@pytest.fixture
def expired_token():
    """Returns a function that makes a correctly signed token for a user that expired a minute ago."""

    def make(user: User) -> str:
        claims = {"sub": str(user.id), "role": user.role.value, "exp": datetime.now(timezone.utc) - timedelta(minutes=1)}
        return jwt.encode(claims, settings.jwt_secret, algorithm=settings.jwt_algorithm)

    return make


@pytest.fixture
def tampered_token():
    """Returns a function that makes a valid token and then changes the first character of its signature."""

    def make(user: User) -> str:
        header, payload, signature = create_access_token(user).split(".")
        return ".".join([header, payload, ("A" if signature[0] != "A" else "B") + signature[1:]])

    return make


class QuietServer(uvicorn.Server):
    # uvicorn would install its own SIGINT/SIGTERM handlers and break Ctrl+C for pytest.
    @contextlib.contextmanager
    def capture_signals(self):
        yield


# Written out by hand, not imported from the app, so a wrong change to the channel name fails the tests.
TEST_CHANNEL = f"ws:events:{TEST_REDIS_DB}"


async def wait_for_subscribers(count: int) -> None:
    """Waits until `count` pub/sub clients are subscribed to the test channel."""
    deadline = time.monotonic() + 5
    while (await redis_client.pubsub_numsub(TEST_CHANNEL))[0][1] != count:
        assert time.monotonic() < deadline, f"expected {count} subscriber(s) on {TEST_CHANNEL}"
        await asyncio.sleep(0.02)


@pytest_asyncio.fixture
async def live_server(test_engine):
    """The real app under uvicorn on a free port in the test's own event loop. Returns its ws:// URL.
    Its lifespan runs, so the WebSocket listener and the offer sweeper run too.

    httpx cannot speak WebSocket, and Starlette's TestClient runs the app in another thread and loop,
    which clashes with asyncpg and the Redis client.
    """
    assert websocket_router.connections == {}

    server = QuietServer(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning", ws_max_size=65536))
    task = asyncio.create_task(server.serve())
    while not server.started:
        assert not task.done(), "the test server stopped while starting"
        await asyncio.sleep(0.01)
    port = server.servers[0].sockets[0].getsockname()[1]
    await wait_for_subscribers(1)

    yield f"ws://127.0.0.1:{port}/ws"

    server.should_exit = True
    await task
    await wait_for_subscribers(0)


@pytest.fixture
def wait_for_listener():
    """Returns the function that waits until `count` pub/sub clients are subscribed to the test channel."""
    return wait_for_subscribers
