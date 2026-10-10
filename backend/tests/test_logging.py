import asyncio
import io
import json
import logging
import re

import pytest
from fastapi.routing import APIRoute
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.config import settings
from app.main import app
from app.models import RideStatus, User, UserRole
from app.observability.context import request_context
from app.observability.logs import FIELD_WHITELIST, JsonFormatter
from app.services import rides as rides_service
from test_rides import RIDE_BODY
from test_websocket import auth_frame, open_socket  # noqa: F401  (open_socket is a fixture)

# Written out by hand, not imported from the app, so a wrong change to the whitelist fails the tests.
EXPECTED_WHITELIST = {
    "request_id", "user_id", "role", "component", "ride_id", "driver_id", "offer_id", "method", "route", "status",
    "duration_ms", "db_queries", "db_ms", "from_status", "to_status", "ws_close_code", "sqlstate", "constraint", "exc_type",
    "stack", "service", "outcome", "count",
}
BASE_KEYS = {"ts", "level", "logger", "msg"}


@pytest.fixture
def log_lines():
    """Captures every log record of the process as parsed JSON (the app's own formatter). Returns a function that gives the
    lines so far; its `raw` attribute gives the text. Also puts uvicorn's loggers back on the root logger and at the level
    production has, because a test server started by uvicorn.Config replaces their handlers and sets a level."""
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    root, uvicorn_logger, uvicorn_error = logging.getLogger(), logging.getLogger("uvicorn"), logging.getLogger("uvicorn.error")
    saved = (root.level, uvicorn_logger.handlers, uvicorn_logger.propagate, uvicorn_error.level)
    root.addHandler(handler)
    root.setLevel(logging.DEBUG)
    uvicorn_logger.handlers, uvicorn_logger.propagate = [], True
    uvicorn_error.setLevel(logging.NOTSET)  # a test server started with log_level="warning" would hide the INFO lines

    def lines() -> list[dict]:
        return [json.loads(line) for line in stream.getvalue().splitlines()]

    lines.raw = stream.getvalue
    yield lines
    root.removeHandler(handler)
    root.setLevel(saved[0])
    uvicorn_logger.handlers, uvicorn_logger.propagate = saved[1], saved[2]
    uvicorn_error.setLevel(saved[3])


@pytest.fixture
def add_route():
    """Returns a function that adds a GET route in front of everything (the frontend mount matches every path), and removes it afterwards."""
    added = []

    def add(path: str, endpoint, **options) -> None:
        route = APIRoute(path, endpoint, methods=["GET"], **options)
        app.router.routes.insert(0, route)
        added.append(route)

    yield add
    for route in added:
        app.router.routes.remove(route)


def access_lines(lines: list[dict]) -> list[dict]:
    return [line for line in lines if line["msg"] == "http_request"]


# --- the formatter ---


def test_every_record_is_one_line_of_json(log_lines):
    logging.getLogger("test.formatter").info('first "quoted" line\nsecond line')

    assert log_lines.raw().count("\n") == 1
    line = log_lines()[0]
    assert set(line) == BASE_KEYS
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z", line["ts"])
    assert (line["level"], line["logger"], line["msg"]) == ("INFO", "test.formatter", 'first "quoted" line\nsecond line')


def test_unknown_extra_keys_are_dropped(log_lines):
    logging.getLogger("test.formatter").info(
        "event", extra={"email": "a@example.com", "password": "pw", "otp": "1234", "token": "tok", "authorization": "Bearer x",
                        "address": "MG Road", "ride_id": 5}
    )

    line = log_lines()[0]
    assert set(line) == BASE_KEYS | {"ride_id"}
    for secret in ("a@example.com", "pw", "1234", "tok", "Bearer", "MG Road"):
        assert secret not in log_lines.raw()


def test_every_whitelisted_key_is_kept(log_lines):
    assert set(FIELD_WHITELIST) == EXPECTED_WHITELIST
    logging.getLogger("test.formatter").info("event", extra={key: f"value-{key}" for key in EXPECTED_WHITELIST})

    assert set(log_lines()[0]) == BASE_KEYS | EXPECTED_WHITELIST


def test_an_exception_logs_its_type_and_stack_but_not_its_message(log_lines):
    secret = "secret@" + "example.com"  # built at run time, because the source line of a stack frame is part of the stack
    try:
        raise ValueError(secret)
    except ValueError:
        logging.getLogger("test.formatter").error("failed", exc_info=True)

    line = log_lines()[0]
    assert line["exc_type"] == "ValueError"
    assert "test_logging.py" in line["stack"]
    assert secret not in log_lines.raw()


async def test_a_database_error_logs_the_sqlstate_and_the_constraint_but_no_values(db, log_lines):
    email = "dup-" + "x@example.com"
    db.add(User(role=UserRole.rider, name="One", email=email, password_hash="x"))
    await db.flush()
    db.add(User(role=UserRole.rider, name="Two", email=email, password_hash="x"))
    try:
        await db.flush()
    except IntegrityError:
        logging.getLogger("test.formatter").error("failed", exc_info=True)
    await db.rollback()

    line = log_lines()[0]
    assert (line["exc_type"], line["sqlstate"], line["constraint"]) == ("IntegrityError", "23505", "uq_users_email")
    assert email not in log_lines.raw()


def test_an_error_inside_the_formatter_does_not_propagate():
    record = logging.LogRecord("test.formatter", logging.INFO, __file__, 1, "%s and %s", (1,), None)  # too few arguments

    line = json.loads(JsonFormatter().format(record))

    assert line["msg"] == "log_format_error"


# --- the request id ---


async def test_a_request_id_is_generated_and_returned(client):
    response = await client.get("/health")

    assert re.fullmatch(r"[0-9a-f]{32}", response.headers["x-request-id"])


@pytest.mark.parametrize("value", ["abcdefgh", "A1b2.C3_d4-e5", "a" * 64])
async def test_a_valid_inbound_request_id_is_kept(client, value):
    response = await client.get("/health", headers={"X-Request-ID": value})

    assert response.headers["x-request-id"] == value


@pytest.mark.parametrize("value", ["short", "a" * 65, "has a space in it", "newline\nin-the-id", 'quote"in-the-id', ""])
async def test_a_bad_inbound_request_id_is_replaced(client, value):
    response = await client.get("/health", headers={"X-Request-ID": value})

    assert re.fullmatch(r"[0-9a-f]{32}", response.headers["x-request-id"])


async def test_the_same_request_id_is_on_every_line_of_a_request(client, log_lines, rider, driver, put_online):
    await put_online(driver, RIDE_BODY["pickup_lat"], RIDE_BODY["pickup_lng"])
    before = len(log_lines())

    response = await client.post("/rides", json=RIDE_BODY, headers={**rider["headers"], "X-Request-ID": "ride-request-0001"})

    assert response.status_code == 201
    lines = log_lines()[before:]
    # The offer line comes from a service, the transition line from the commit hook, the access line from the middleware.
    assert {line["msg"] for line in lines} >= {"ride_transition", "http_request"}
    assert any(line["msg"].startswith("Offer ") for line in lines)
    assert {line["request_id"] for line in lines} == {"ride-request-0001"}


async def test_twenty_concurrent_requests_never_mix_request_ids(client, log_lines, add_route):
    entered = 0
    release = asyncio.Event()

    async def wait_for_release(n: int) -> dict:
        nonlocal entered
        entered += 1
        await release.wait()
        logging.getLogger("test.concurrent").info("inside", extra={"count": n})
        return {"n": n}

    add_route("/__wait", wait_for_release)

    tasks = [asyncio.create_task(client.get(f"/__wait?n={n}", headers={"X-Request-ID": f"request-{n:04d}"})) for n in range(20)]
    while entered < 20:
        await asyncio.sleep(0.01)
    release.set()
    responses = await asyncio.gather(*tasks)

    assert [response.headers["x-request-id"] for response in responses] == [f"request-{n:04d}" for n in range(20)]
    inside = [line for line in log_lines() if line["msg"] == "inside"]
    assert len(inside) == 20
    assert all(line["request_id"] == f"request-{line['count']:04d}" for line in inside)


async def test_the_request_id_header_is_on_error_responses_too(client, add_route):
    def broken() -> None:
        raise RuntimeError("broken")

    add_route("/__broken", broken)

    responses = [
        await client.get("/no/such/path"),  # 404
        await client.get("/auth/me"),  # 401
        await client.post("/auth/login", json={"email": "not-an-email"}),  # 422
        await client.get("/__broken"),  # 500
    ]

    assert [response.status_code for response in responses] == [404, 401, 422, 500]
    assert all(re.fullmatch(r"[0-9a-f]{32}", response.headers["x-request-id"]) for response in responses)


# --- the access line ---


async def test_one_access_line_per_request_with_the_documented_fields(client, log_lines, rider, insert_ride):
    ride = await insert_ride(rider, RideStatus.REQUESTED)
    before = len(log_lines())

    response = await client.get(f"/rides/{ride.id}", headers=rider["headers"])

    lines = access_lines(log_lines()[before:])
    assert len(lines) == 1
    line = lines[0]
    assert {key: line[key] for key in ("method", "route", "status", "ride_id", "user_id", "role", "level")} == {
        "method": "GET", "route": "/rides/{ride_id}", "status": 200, "ride_id": ride.id, "user_id": rider["user"].id,
        "role": "rider", "level": "INFO",
    }
    assert line["request_id"] == response.headers["x-request-id"]
    assert line["db_queries"] >= 2 and line["db_ms"] > 0  # the user lookup and the ride
    assert isinstance(line["duration_ms"], float) and line["duration_ms"] > 0
    assert f"/rides/{ride.id}" not in log_lines.raw()


async def test_ride_id_is_logged_only_for_ride_id_routes_with_digits(client, log_lines, rider):
    await client.get("/rides/active", headers=rider["headers"])  # no ride_id parameter
    await client.get("/rides/abc", headers=rider["headers"])  # a parameter that is not digits (422)
    await client.get("/rides/99999", headers=rider["headers"])  # digits, no such ride (404)

    lines = access_lines(log_lines())
    assert [(line["route"], line["status"]) for line in lines] == [("/rides/active", 404), ("/rides/{ride_id}", 422), ("/rides/{ride_id}", 404)]
    assert "ride_id" not in lines[0] and "ride_id" not in lines[1]
    assert lines[2]["ride_id"] == 99999
    assert "abc" not in log_lines.raw()


async def test_a_query_string_is_never_logged(client, log_lines):
    await client.get("/health?q=secret@example.com&token=abc")
    await client.get("/rides/history?status=COMPLETED&since=secret@example.com", headers={"Authorization": "Bearer x"})

    assert "secret@example.com" not in log_lines.raw()
    assert "token=abc" not in log_lines.raw()


async def test_an_unknown_path_is_logged_as_unmatched_without_the_path(client, log_lines):
    response = await client.get("/definitely/not/a/page-9f3a")

    assert response.status_code == 404
    line = access_lines(log_lines())[0]
    assert (line["route"], line["status"]) == ("unmatched", 404)
    assert "page-9f3a" not in log_lines.raw()


async def test_static_files_are_logged_as_static(client, log_lines):
    response = await client.get("/rider/")

    assert response.status_code == 200
    assert access_lines(log_lines())[0]["route"] == "static"


@pytest.mark.parametrize("role", ["rider", "driver", "admin"])
async def test_the_user_and_role_are_on_the_access_line_of_an_authenticated_request(client, log_lines, make_user, role):
    who = await make_user(role)

    await client.get("/auth/me", headers=who["headers"])

    line = access_lines(log_lines())[-1]
    assert (line["route"], line["user_id"], line["role"]) == ("/auth/me", who["user"].id, role)


async def test_an_anonymous_request_has_no_user_on_its_access_line(client, log_lines):
    await client.get("/health")
    await client.get("/auth/me")  # refused: no token

    for line in access_lines(log_lines()):
        assert "user_id" not in line and "role" not in line


async def test_a_value_set_in_a_threadpool_dependency_reaches_the_access_line(client, log_lines, add_route):
    from fastapi import Depends

    def set_user_in_a_worker_thread() -> None:  # a plain def: FastAPI runs it in a thread
        request_context.get().update(user_id=777, role="rider")

    def endpoint(_: None = Depends(set_user_in_a_worker_thread)) -> dict:
        return {}

    add_route("/__thread", endpoint)

    await client.get("/__thread")

    line = access_lines(log_lines())[0]
    assert (line["user_id"], line["role"]) == (777, "rider")


async def test_the_level_follows_the_status_and_the_route(client, log_lines, add_route, monkeypatch):
    def broken() -> None:
        raise RuntimeError("broken")

    add_route("/__broken", broken)
    monkeypatch.setattr(settings, "metrics_token", "")

    await client.get("/health")
    await client.get("/metrics")
    await client.get("/auth/me")  # 401
    await client.post("/auth/login", json={"email": "not-an-email"})  # 422
    await client.get("/__broken")

    levels = {(line["route"], line["status"]): line["level"] for line in access_lines(log_lines())}
    assert levels == {
        ("/health", 200): "DEBUG", ("/metrics", 404): "DEBUG", ("/auth/me", 401): "INFO", ("/auth/login", 422): "INFO",
        ("/__broken", 500): "ERROR",
    }


# --- an unhandled exception ---


async def test_an_unhandled_exception_gives_a_generic_500_and_one_error_line(client, log_lines, rider, monkeypatch):
    secret = "secret@" + "example.com"

    async def broken(*args, **kwargs):
        raise RuntimeError(secret)

    monkeypatch.setattr(rides_service, "get_active", broken)

    response = await client.get("/rides/active", headers=rider["headers"])

    assert response.status_code == 500
    assert response.text == "Internal Server Error"
    assert secret not in log_lines.raw()
    errors = [line for line in log_lines() if line["level"] == "ERROR"]
    assert len(errors) == 1
    assert errors[0]["msg"] == "http_request"
    assert (errors[0]["status"], errors[0]["exc_type"]) == (500, "RuntimeError")
    assert errors[0]["request_id"] == response.headers["x-request-id"]


# --- the privacy scan ---


# open_socket first: starting the test server resets uvicorn's loggers, so the capture must start after it.
async def test_nothing_personal_reaches_the_logs(open_socket, client, log_lines, make_user, monkeypatch):
    otp = "0451"  # a code that no id, time, or count can look like
    monkeypatch.setattr(rides_service, "FAKE_OTP", otp)
    monkeypatch.setattr(settings, "stripe_secret_key", "")  # the top-up is refused as "not configured": no network call
    rider_email, rider_password = "scan-rider-" + "7f3a@scan-example.org", "RiderPassw0rd-" + "x1"
    driver_email, driver_password = "scan-driver-" + "7f3a@scan-example.org", "DriverPassw0rd-" + "x1"
    license_number, plate = "LICSCAN" + "93117", "KA01SC" + "4821"
    address, comment = "17 Scan Street Unique" + "Quarter", "Unique comment " + "about the ride 5521"
    phone = "+9199" + "88877766"

    for email, password, role in ((rider_email, rider_password, "rider"), (driver_email, driver_password, "driver")):
        registered = await client.post(
            "/auth/register", json={"name": "Scan " + role, "email": email, "password": password, "role": role, "phone": phone if role == "driver" else None}
        )
        assert registered.status_code == 201, registered.text
    tokens = {}
    for email, password, role in ((rider_email, rider_password, "rider"), (driver_email, driver_password, "driver")):
        login = await client.post("/auth/login", json={"email": email, "password": password})
        tokens[role] = {"Authorization": "Bearer " + login.json()["access_token"]}
    failed = await client.post("/auth/login", json={"email": rider_email, "password": "WrongPassw0rd-" + "zz"})
    assert failed.status_code == 401
    unprocessable = await client.post("/auth/login", json={"email": "bad@@" + "scan-example.org", "password": "x"})
    assert unprocessable.status_code == 422

    admin = await make_user("admin")
    profile = await client.post("/drivers/me/profile", json={"license_number": license_number}, headers=tokens["driver"])
    assert profile.status_code == 201, profile.text
    vehicle = await client.post("/drivers/me/vehicle", json={"plate_number": plate, "model": "Swift", "color": "white"}, headers=tokens["driver"])
    assert vehicle.status_code == 201, vehicle.text
    approved = await client.post(f"/admin/drivers/{profile.json()['id']}/approve", headers=admin["headers"])
    assert approved.status_code == 200, approved.text

    saved = await client.post("/saved-places", json={"label": "Scan home", "address": address, "lat": 12.9716, "lng": 77.5946}, headers=tokens["rider"])
    assert saved.status_code == 201, saved.text

    online = await client.post("/drivers/me/online", json={"lat": RIDE_BODY["pickup_lat"], "lng": RIDE_BODY["pickup_lng"]}, headers=tokens["driver"])
    assert online.status_code == 200, online.text
    ride = await client.post("/rides", json={**RIDE_BODY, "pickup_address": address, "dropoff_address": address + " Two"}, headers=tokens["rider"])
    assert ride.status_code == 201, ride.text
    ride_id = ride.json()["id"]
    offer = await client.get("/drivers/me/offer", headers=tokens["driver"])
    assert (await client.post(f"/offers/{offer.json()['id']}/accept", headers=tokens["driver"])).status_code == 200
    assert (await client.post(f"/rides/{ride_id}/arrive", headers=tokens["driver"])).status_code == 200
    shown = await client.get(f"/rides/{ride_id}/otp", headers=tokens["rider"])
    assert shown.json()["otp"] == otp
    assert (await client.post(f"/rides/{ride_id}/start", json={"otp": otp}, headers=tokens["driver"])).status_code == 200
    assert (await client.post(f"/rides/{ride_id}/complete", headers=tokens["driver"])).status_code == 200
    rating = await client.post(f"/rides/{ride_id}/rating", json={"score": 5, "comment": comment}, headers=tokens["rider"])
    assert rating.status_code == 201, rating.text
    await client.post("/wallet/topups", json={"amount": 30000}, headers={**tokens["rider"], "Idempotency-Key": "scan-topup-key-0001"})
    rules = (await client.get("/admin/pricing-rules", headers=admin["headers"])).json()
    edit = await client.patch("/admin/pricing-rules/economy", json={"version": rules["rules"][0]["version"], "per_km": 1300}, headers=admin["headers"])
    assert edit.status_code == 200, edit.text

    ws = await open_socket()
    await ws.send(auth_frame(tokens["rider"]["Authorization"].removeprefix("Bearer ")))
    assert json.loads(await ws.recv())["type"] == "auth_ok"
    await ws.close()
    await asyncio.sleep(0.2)  # the server's closing line

    text = log_lines.raw()
    secrets = [
        rider_email, driver_email, "scan-example.org", rider_password, driver_password, "WrongPassw0rd", "Bearer", "eyJ",
        license_number, plate, "UniqueQuarter", "Unique comment", "about the ride", phone, "Swift", "MG Road", "Koramangala",
    ]
    for secret in secrets + [tokens["rider"]["Authorization"].removeprefix("Bearer "), tokens["driver"]["Authorization"].removeprefix("Bearer ")]:
        assert secret not in text, secret
    assert not re.search(r"(?<![\d.])" + otp + r"(?!\d)", text)
    assert not re.search(r"-?\d+\.\d{4,}", text)  # no coordinate
    for line in log_lines():
        assert set(line) <= BASE_KEYS | EXPECTED_WHITELIST, line
    assert [line["msg"] for line in log_lines() if line["msg"].startswith("ws_")] == ["ws_connected", "ws_closed"]
