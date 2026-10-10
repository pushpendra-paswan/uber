import asyncio
import hmac
import logging
import types
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from fastapi import HTTPException
from prometheus_client import REGISTRY, generate_latest
from redis.exceptions import RedisError
from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlalchemy.exc import OperationalError

from app.config import settings
from app.models import OfferStatus, RideOffer, RideStatus
from app.observability import hooks
from app.observability.metrics import timed_external
from app.repositories import admin as admin_repo
from app.repositories import drivers as drivers_repo
from app.repositories import places as places_repo
from app.routers import websocket as websocket_router
from app.services import offers as offers_service
from app.services import payments, places, routing
from test_logging import access_lines, add_route, log_lines  # noqa: F401  (the last two are fixtures)
from test_rides import RIDE_BODY
from test_websocket import auth_frame, close_code, eventually, open_socket, token_of  # noqa: F401  (open_socket is a fixture)

REAL_GET_ROUTE = routing.get_route  # read while the autouse fixture has not replaced it yet
METRICS_TOKEN = "test-metrics-token-0123456789abcdef"
METRIC_NAMES = (
    "http_requests_total", "http_request_duration_seconds", "http_requests_in_flight", "db_query_duration_seconds",
    "db_pool_size", "db_pool_checked_out", "db_pool_overflow", "external_call_duration_seconds", "ws_connections",
    "ws_closes_total", "ride_transitions_total", "rides_active", "offers_pending", "drivers_online",
    "sweeper_tick_duration_seconds", "sweeper_last_success_timestamp_seconds", "sweeper_errors_total", "observability_errors_total",
)


def sample(name: str, **labels) -> float:
    return REGISTRY.get_sample_value(name, labels) or 0


def series(name: str) -> set[tuple]:
    """Every label set of a metric family that exists right now."""
    return {tuple(sorted(s.labels.items())) for family in REGISTRY.collect() for s in family.samples if s.name == name}


# --- /metrics ---


async def test_metrics_is_off_without_a_token(client, monkeypatch):
    monkeypatch.setattr(settings, "metrics_token", "")

    response = await client.get("/metrics", headers={"Authorization": f"Bearer {METRICS_TOKEN}"})

    assert response.status_code == 404


async def test_metrics_needs_exactly_the_token(client, admin, monkeypatch):
    monkeypatch.setattr(settings, "metrics_token", METRICS_TOKEN)
    bad_headers = [
        {},
        {"Authorization": "Basic dGVzdDp0ZXN0"},
        {"Authorization": "Bearer wrong-token"},
        {"Authorization": f"Bearer {METRICS_TOKEN[:-1]}"},  # a prefix of the right token
        {"Authorization": f"Bearer {METRICS_TOKEN}x"},
        {"Authorization": METRICS_TOKEN},
        admin["headers"],  # a valid user token
    ]

    responses = [await client.get("/metrics", headers=headers) for headers in bad_headers]

    assert [response.status_code for response in responses] == [401] * len(bad_headers)
    assert {response.text for response in responses} == {"Unauthorized"}


async def test_metrics_with_the_token_lists_every_metric(client, monkeypatch):
    monkeypatch.setattr(settings, "metrics_token", METRICS_TOKEN)

    response = await client.get("/metrics", headers={"Authorization": f"Bearer {METRICS_TOKEN}"})

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain; version=") and response.headers["content-type"].endswith("; charset=utf-8")
    for name in METRIC_NAMES:
        assert f"# TYPE {name} " in response.text, name
    assert "process_cpu_seconds_total" in response.text  # the default collectors come free


async def test_metrics_compares_the_token_with_compare_digest(client, log_lines, monkeypatch):
    monkeypatch.setattr(settings, "metrics_token", METRICS_TOKEN)
    calls = []
    real = hmac.compare_digest
    monkeypatch.setattr(hmac, "compare_digest", lambda a, b: calls.append((a, b)) or real(a, b))

    await client.get("/metrics", headers={"Authorization": f"Bearer {METRICS_TOKEN}"})
    await client.get("/metrics", headers={"Authorization": "Bearer wrong-token-value"})

    assert len(calls) == 2
    assert METRICS_TOKEN not in log_lines.raw() and "wrong-token-value" not in log_lines.raw()


# --- HTTP metrics ---


async def test_a_request_is_counted_once_under_its_route_template(client, rider):
    labels = {"method": "GET", "route": "/auth/me"}
    before = (sample("http_requests_total", **labels, status="200"), sample("http_request_duration_seconds_count", **labels))

    await client.get("/auth/me", headers=rider["headers"])

    assert sample("http_requests_total", **labels, status="200") == before[0] + 1
    assert sample("http_request_duration_seconds_count", **labels) == before[1] + 1


async def test_a_client_error_is_counted_with_its_status(client):
    before = sample("http_requests_total", method="GET", route="/auth/me", status="401")

    await client.get("/auth/me")

    assert sample("http_requests_total", method="GET", route="/auth/me", status="401") == before + 1


async def test_unknown_paths_and_ride_ids_add_no_series(client, rider):
    await client.get("/no/such/path", headers=rider["headers"])  # makes the "unmatched" series exist
    await client.get("/rides/1", headers=rider["headers"])  # and the template one
    before = series("http_requests_total")

    for n in range(1000):
        await client.get(f"/unknown/{n}/page-{n}")
    for ride_id in range(2, 202):
        await client.get(f"/rides/{ride_id}", headers=rider["headers"])
    await client.request("FOO", "/health")  # a method nobody sent before

    added = series("http_requests_total") - before
    assert {(dict(labels)["method"], dict(labels)["route"]) for labels in added} <= {("OTHER", "static"), ("OTHER", "/health"), ("GET", "unmatched"), ("GET", "/rides/{ride_id}")}
    assert not any(dict(labels)["route"].startswith(("/unknown", "/rides/2")) for labels in series("http_requests_total"))


async def test_the_in_flight_gauge_follows_a_running_request(client, add_route):
    release = asyncio.Event()
    entered = asyncio.Event()

    async def blocked() -> dict:
        entered.set()
        await release.wait()
        return {}

    async def broken() -> dict:
        raise RuntimeError("broken")

    add_route("/__blocked", blocked)
    add_route("/__broken", broken)
    before = sample("http_requests_in_flight")

    task = asyncio.create_task(client.get("/__blocked"))
    await entered.wait()
    assert sample("http_requests_in_flight") == before + 1
    release.set()
    await task
    assert sample("http_requests_in_flight") == before

    await client.get("/__broken")  # raises inside the endpoint
    assert sample("http_requests_in_flight") == before


async def test_an_unhandled_exception_is_counted_as_500(client, add_route):
    async def broken() -> dict:
        raise RuntimeError("broken")

    add_route("/__broken", broken)
    labels = {"method": "GET", "route": "/__broken"}
    before = {status: sample("http_requests_total", **labels, status=status) for status in ("500", "200")}

    await client.get("/__broken")

    assert sample("http_requests_total", **labels, status="500") == before["500"] + 1
    assert sample("http_requests_total", **labels, status="200") == before["200"]


# --- database timing ---


async def test_db_queries_and_db_ms_match_what_the_request_really_ran(client, log_lines, rider):
    statements = []

    def count(connection, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(Engine, "before_cursor_execute", count)
    queries_before = sample("db_query_duration_seconds_count")
    try:
        await client.get("/auth/me", headers=rider["headers"])
    finally:
        event.remove(Engine, "before_cursor_execute", count)

    line = access_lines(log_lines())[-1]
    assert len(statements) >= 1
    assert line["db_queries"] == len(statements)  # the request context reaches the event inside the async greenlet
    assert line["db_ms"] > 0
    assert sample("db_query_duration_seconds_count") - queries_before == len(statements)


async def test_a_request_without_database_access_has_zero_queries(client, log_lines):
    await client.post("/auth/login", json={"email": "not-an-email"})  # refused by validation

    line = access_lines(log_lines())[-1]
    assert (line["db_queries"], line["db_ms"]) == (0, 0.0)


# --- external calls ---


def mock_httpx(monkeypatch, handler) -> None:
    """Every httpx.AsyncClient the app creates from now on talks to `handler` instead of the network."""
    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **options: real(transport=httpx.MockTransport(handler), **options))


def answer(kind: str, success: httpx.Response):
    secret_url = "http://secret-host.example/private?q=secret-search"

    def handler(request: httpx.Request) -> httpx.Response:
        if kind == "ok":
            return success
        if kind == "http500":
            return httpx.Response(500, text="boom")
        if kind == "connect":
            raise httpx.ConnectError(f"cannot reach {secret_url}")
        raise httpx.ReadTimeout(f"timed out reading {secret_url}")

    return handler


OSRM_OK = httpx.Response(200, json={"code": "Ok", "routes": [{"distance": 1000, "duration": 100, "geometry": {"coordinates": [[77.5, 12.9], [77.6, 13.0]]}}]})


@pytest.mark.parametrize(
    "kind, outcome, fails", [("ok", "ok", False), ("http500", "error", True), ("connect", "error", True), ("timeout", "timeout", True)]
)
@pytest.mark.parametrize("service", ["osrm", "nominatim", "stripe"])
async def test_external_calls_are_timed_by_service_and_outcome(service, kind, outcome, fails, log_lines, monkeypatch):
    monkeypatch.setattr(routing, "get_route", REAL_GET_ROUTE)
    monkeypatch.setattr(places_repo, "acquire_slot", lambda: _true())
    monkeypatch.setattr(settings, "stripe_secret_key", "sk_test_unit")
    successes = {"osrm": OSRM_OK, "nominatim": httpx.Response(200, json=[]), "stripe": httpx.Response(200, json={"id": "cs_1"})}
    mock_httpx(monkeypatch, answer(kind, successes[service]))
    calls = {
        "osrm": lambda: routing.get_route(12.9, 77.5, 13.0, 77.6),
        "nominatim": lambda: places.call_nominatim("/search", {"q": "secret-search"}),
        "stripe": lambda: payments.call_stripe("GET", "/v1/checkout/sessions"),
    }
    before = sample("external_call_duration_seconds_count", service=service, outcome=outcome)

    if fails:
        with pytest.raises(HTTPException) as error:
            await calls[service]()
        assert error.value.status_code == 502
    else:
        await calls[service]()

    assert sample("external_call_duration_seconds_count", service=service, outcome=outcome) == before + 1
    # No URL, query, or message in a label or a log line.
    text = generate_latest().decode() + log_lines.raw()
    assert "secret-host" not in text and "secret-search" not in text


async def _true() -> bool:
    return True


async def test_timed_external_passes_the_exception_through_unchanged():
    error = httpx.ReadTimeout("slow")

    with pytest.raises(httpx.ReadTimeout) as caught:
        with timed_external("osrm"):
            raise error

    assert caught.value is error


# --- WebSockets ---


async def test_ws_connections_follows_connect_and_close(open_socket, make_user):
    who = await make_user("rider")
    before = sample("ws_connections")

    ws = await open_socket(who)
    assert sample("ws_connections") == before + 1
    await ws.close()

    await eventually(lambda: sample("ws_connections") == before)
    assert sample("ws_closes_total", code="1000") >= 1


async def test_twenty_abruptly_dropped_connections_return_the_gauge_to_its_start(open_socket, make_user):
    users = [await make_user("rider") for _ in range(4)]
    before = sample("ws_connections")
    closes_before = sample("ws_closes_total", code="1006")

    sockets = [await open_socket(who) for who in users for _ in range(5)]
    assert sample("ws_connections") == before + 20
    for ws in sockets:
        ws.transport.abort()  # no close frame

    await eventually(lambda: sample("ws_connections") == before, seconds=5)
    assert sample("ws_closes_total", code="1006") - closes_before == 20


async def test_close_codes_are_counted_by_code(open_socket, make_user, monkeypatch):
    monkeypatch.setattr(websocket_router, "AUTH_TIMEOUT_SECONDS", 0.2)
    who = await make_user("rider")
    before = {code: sample("ws_closes_total", code=code) for code in ("4400", "4401", "4408", "4409", "other")}

    junk = await open_socket()
    await junk.send("not json")  # the first frame is not an auth frame
    assert await close_code(junk) == 4401
    bad_token = await open_socket()
    await bad_token.send(auth_frame("not-a-token"))
    assert await close_code(bad_token) == 4401
    silent = await open_socket()
    assert await close_code(silent, timeout=3) == 4408
    bad_message = await open_socket(who)
    await bad_message.send("not json")
    assert await close_code(bad_message) == 4400
    first = await open_socket(who)
    for _ in range(5):  # the sixth connection of one user replaces the oldest
        await open_socket(who)
    assert await close_code(first) == 4409
    odd = await open_socket(await make_user("rider"))
    await odd.close(code=4000)

    await eventually(lambda: sample("ws_closes_total", code="other") == before["other"] + 1)
    await eventually(lambda: sample("ws_closes_total", code="4409") == before["4409"] + 1)
    assert sample("ws_closes_total", code="4401") == before["4401"] + 2
    assert sample("ws_closes_total", code="4408") == before["4408"] + 1
    assert sample("ws_closes_total", code="4400") == before["4400"] + 1


async def test_the_websocket_log_lines_carry_the_request_id_and_close_code_but_never_the_token(open_socket, log_lines, make_user):
    who = await make_user("rider")

    ws = await open_socket(who)
    await ws.close()
    await eventually(lambda: any(line["msg"] == "ws_closed" for line in log_lines()))

    connected = next(line for line in log_lines() if line["msg"] == "ws_connected")
    closed = next(line for line in log_lines() if line["msg"] == "ws_closed")
    assert connected["request_id"] == closed["request_id"]
    assert (closed["ws_close_code"], closed["user_id"], closed["role"]) == (1000, who["user"].id, "rider")
    assert token_of(who) not in log_lines.raw()


# --- the gauge task ---


async def one_refresh(monkeypatch) -> None:
    """Runs the gauge loop for exactly one iteration: the sleep after it raises, which ends the loop."""

    async def stop(seconds: float) -> None:
        raise asyncio.CancelledError

    monkeypatch.setattr(hooks, "asyncio", types.SimpleNamespace(sleep=stop))
    with pytest.raises(asyncio.CancelledError):
        await hooks.refresh_gauges_forever()


async def test_one_refresh_sets_the_business_gauges_from_the_database_and_redis(db, make_user, insert_ride, monkeypatch):
    riders = [await make_user("rider") for _ in range(8)]
    drivers = [await make_user("driver") for _ in range(6)]
    await insert_ride(riders[0], RideStatus.REQUESTED)
    await insert_ride(riders[1], RideStatus.DRIVER_ASSIGNED, drivers[0])
    await insert_ride(riders[2], RideStatus.DRIVER_ASSIGNED, drivers[1])
    await insert_ride(riders[3], RideStatus.DRIVER_ARRIVED, drivers[2])
    await insert_ride(riders[4], RideStatus.IN_PROGRESS, drivers[3])
    await insert_ride(riders[5], RideStatus.COMPLETED, drivers[4])  # terminal: not counted
    await insert_ride(riders[6], RideStatus.CANCELLED)
    requested_2 = await insert_ride(riders[7], RideStatus.REQUESTED)
    # Two pending offers, one of them past its deadline and not yet swept.
    pending = await insert_ride(riders[6], RideStatus.NO_DRIVER_FOUND)
    now = datetime.now(timezone.utc)
    db.add(RideOffer(ride_id=requested_2.id, driver_id=drivers[4]["driver"].id, status=OfferStatus.PENDING, pickup_distance_m=100, expires_at=now + timedelta(seconds=10)))
    db.add(RideOffer(ride_id=pending.id, driver_id=drivers[5]["driver"].id, status=OfferStatus.PENDING, pickup_distance_m=100, expires_at=now - timedelta(seconds=10)))
    await db.commit()
    for who in drivers[:4]:
        await drivers_repo.set_online(who["driver"].id, RIDE_BODY["pickup_lat"], RIDE_BODY["pickup_lng"])

    await one_refresh(monkeypatch)

    assert {status: sample("rides_active", status=status) for status in ("REQUESTED", "DRIVER_ASSIGNED", "DRIVER_ARRIVED", "IN_PROGRESS")} == {
        "REQUESTED": 2, "DRIVER_ASSIGNED": 2, "DRIVER_ARRIVED": 1, "IN_PROGRESS": 1,
    }
    assert sample("offers_pending") == 2
    assert sample("drivers_online") == 4
    # The pool gauges are there, and a connection in use is one the pool has created.
    assert sample("db_pool_checked_out") <= sample("db_pool_size") + sample("db_pool_overflow")


async def test_a_redis_failure_keeps_drivers_online_and_still_updates_the_database_gauges(db, make_user, insert_ride, monkeypatch):
    await insert_ride(await make_user("rider"), RideStatus.REQUESTED)
    hooks.metrics.drivers_online.set(7)
    hooks.metrics.rides_active.labels("REQUESTED").set(0)
    errors_before = sample("observability_errors_total", source="gauges_redis")

    async def down():
        raise RedisError("down")

    monkeypatch.setattr(drivers_repo, "get_online_positions", down)
    await one_refresh(monkeypatch)

    assert sample("drivers_online") == 7
    assert sample("observability_errors_total", source="gauges_redis") == errors_before + 1
    assert sample("rides_active", status="REQUESTED") == 1


async def test_a_postgres_failure_keeps_the_database_gauges_and_still_updates_drivers_online(make_user, monkeypatch):
    who = await make_user("driver")
    await drivers_repo.set_online(who["driver"].id, RIDE_BODY["pickup_lat"], RIDE_BODY["pickup_lng"])
    hooks.metrics.rides_active.labels("REQUESTED").set(9)
    errors_before = sample("observability_errors_total", source="gauges_db")

    async def down(db):
        raise OperationalError("select", {}, Exception("down"))

    monkeypatch.setattr(admin_repo, "count_active_rides_by_status", down)
    await one_refresh(monkeypatch)

    assert sample("rides_active", status="REQUESTED") == 9
    assert sample("observability_errors_total", source="gauges_db") == errors_before + 1
    assert sample("drivers_online") == 1


# --- the sweeper heartbeat ---


async def test_the_sweeper_heartbeat_moves_only_after_a_tick_that_did_not_fail(log_lines, monkeypatch):
    secret = "secret@" + "example.com"
    clock = types.SimpleNamespace(now=1000.0)
    clock.time = lambda: clock.now
    clock.perf_counter = lambda: clock.now
    ticks = []
    outcomes = iter(["fails", "works", "works"])

    async def expire_due_offers() -> None:
        clock.now += 1
        if next(outcomes) == "fails":
            raise RuntimeError(secret)

    async def sleep(seconds: float) -> None:  # runs after every tick: look at the gauge, and stop after three ticks
        ticks.append(sample("sweeper_last_success_timestamp_seconds"))
        if len(ticks) == 3:
            raise asyncio.CancelledError

    monkeypatch.setattr(offers_service, "expire_due_offers", expire_due_offers)
    monkeypatch.setattr(offers_service, "time", clock)
    monkeypatch.setattr(offers_service, "asyncio", types.SimpleNamespace(sleep=sleep))
    errors_before, ticks_before = sample("sweeper_errors_total"), sample("sweeper_tick_duration_seconds_count")
    last_before = sample("sweeper_last_success_timestamp_seconds")

    with pytest.raises(asyncio.CancelledError):
        await offers_service.sweep_forever()

    assert ticks == [last_before, 1002.0, 1003.0]  # the failing tick left the heartbeat alone, the next two moved it
    assert sample("sweeper_errors_total") == errors_before + 1
    assert sample("sweeper_tick_duration_seconds_count") == ticks_before + 3
    errors = [line for line in log_lines() if line["msg"] == "sweeper_error"]
    assert len(errors) == 1 and errors[0]["level"] == "ERROR" and errors[0]["exc_type"] == "RuntimeError" and errors[0]["stack"]
    assert secret not in log_lines.raw()
