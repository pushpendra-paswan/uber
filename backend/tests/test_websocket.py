import asyncio
import json
import socket
import time
from urllib.parse import urlparse

import pytest
import pytest_asyncio
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from app.database import redis_client
from app.repositories import events
from app.routers import websocket as websocket_router

# Written out by hand, not imported from the app, so a wrong change to the channel name fails the tests.
CHANNEL = "ws:events:1"
OTHER_DATABASE_CHANNEL = "ws:events:2"


def token_of(who: dict) -> str:
    return who["headers"]["Authorization"].removeprefix("Bearer ")


def auth_frame(token) -> str:
    return json.dumps({"type": "auth", "data": {"token": token}})


async def receive(ws, timeout: float = 2) -> dict:
    return json.loads(await asyncio.wait_for(ws.recv(), timeout))


async def assert_silent(ws, seconds: float = 0.3) -> None:
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(ws.recv(), seconds)


async def close_code(ws, timeout: float = 2) -> int:
    """Waits for the server to close the socket and returns the close code it sent."""
    with pytest.raises(ConnectionClosed):
        await asyncio.wait_for(ws.recv(), timeout)
    return ws.close_code


async def eventually(check, seconds: float = 1.5) -> None:
    deadline = time.monotonic() + seconds
    while not check():
        assert time.monotonic() < deadline, "condition was not reached in time"
        await asyncio.sleep(0.02)


@pytest_asyncio.fixture
async def open_socket(live_server):
    """Returns a function that connects. With a user it also authenticates and checks for auth_ok."""
    opened = []

    async def open_(who: dict | None = None, **options):
        ws = await connect(live_server, **options)
        opened.append(ws)
        if who is not None:
            await ws.send(auth_frame(token_of(who)))
            reply = await receive(ws)
            assert reply["type"] == "auth_ok", reply
        return ws

    yield open_
    for ws in opened:
        await ws.close()


@pytest.mark.parametrize("role", ["rider", "driver", "admin"])
async def test_auth_succeeds_for_every_role(open_socket, make_user, role):
    who = await make_user(role)
    ws = await open_socket()

    await ws.send(auth_frame(token_of(who)))

    assert await receive(ws) == {"type": "auth_ok", "data": {"user_id": who["user"].id, "role": role}}
    assert len(websocket_router.connections[who["user"].id]) == 1


async def test_tokens_that_do_not_belong_to_a_user_are_closed_with_4401(
    open_socket, make_user, db, expired_token, tampered_token
):
    rider = await make_user("rider")
    deleted = await make_user("rider")
    deleted_token = token_of(deleted)
    await db.delete(deleted["user"])
    await db.commit()
    tokens = {
        "garbage": "garbage",
        "expired": expired_token(rider["user"]),
        "tampered": tampered_token(rider["user"]),
        "deleted user": deleted_token,
    }

    for name, token in tokens.items():
        ws = await open_socket()
        await ws.send(auth_frame(token))
        assert await close_code(ws) == 4401, name
        assert websocket_router.connections == {}, name


@pytest.mark.parametrize(
    "first_frame",
    [
        json.dumps({"type": "auth", "data": {}}),
        json.dumps({"type": "auth"}),
        json.dumps({"type": "auth", "data": {"token": 123}}),
        json.dumps({"type": "ping", "data": {}}),
        "not json",
        "[1, 2]",
        b"\x00\x01\x02",
    ],
    ids=["no token", "no data", "token is a number", "ping first", "not json", "json array", "binary"],
)
async def test_a_bad_first_message_is_closed_with_4401(open_socket, first_frame):
    ws = await open_socket()

    await ws.send(first_frame)

    assert await close_code(ws) == 4401
    assert websocket_router.connections == {}


async def test_no_first_message_is_closed_with_4408(open_socket, monkeypatch):
    monkeypatch.setattr(websocket_router, "AUTH_TIMEOUT_SECONDS", 0.3)
    ws = await open_socket()

    assert await close_code(ws) == 4408
    assert websocket_router.connections == {}


async def test_ping_gets_pong_and_an_unknown_type_gets_an_error(open_socket, rider):
    ws = await open_socket(rider)

    await ws.send(json.dumps({"type": "ping", "data": {}}))
    assert await receive(ws) == {"type": "pong", "data": {}}

    await ws.send(json.dumps({"type": "nope", "data": {}}))
    assert (await receive(ws))["type"] == "error"
    await ws.send(json.dumps({"type": "ping", "data": {}}))
    assert await receive(ws) == {"type": "pong", "data": {}}


@pytest.mark.parametrize(
    "frame",
    ["not json", "[1, 2]", json.dumps({"data": {}}), json.dumps({"type": 5}), b"\x00\x01"],
    ids=["invalid json", "json array", "no type", "type is a number", "binary"],
)
async def test_a_bad_message_after_auth_is_closed_with_4400(open_socket, rider, frame):
    ws = await open_socket(rider)

    await ws.send(frame)

    assert await close_code(ws) == 4400


async def test_publish_reaches_only_the_addressed_user(open_socket, rider, driver):
    rider_ws = await open_socket(rider)
    driver_ws = await open_socket(driver)

    subscribers = await events.publish(rider["user"].id, "test", {"n": 1})

    assert subscribers == 1
    assert await receive(rider_ws) == {"type": "test", "data": {"n": 1}}
    await assert_silent(driver_ws)


async def test_two_tabs_both_receive_and_one_close_leaves_the_other(open_socket, rider):
    user_id = rider["user"].id
    first = await open_socket(rider)
    second = await open_socket(rider)

    await events.publish(user_id, "test", {"n": 1})
    assert (await receive(first))["data"] == {"n": 1}
    assert (await receive(second))["data"] == {"n": 1}

    await first.close()
    await eventually(lambda: len(websocket_router.connections[user_id]) == 1)
    await events.publish(user_id, "test", {"n": 2})
    assert (await receive(second))["data"] == {"n": 2}


async def test_publish_to_a_user_who_is_not_connected_is_fine(live_server):
    await events.publish(424242, "test", {"n": 1})


async def test_a_normal_close_removes_the_user(open_socket, rider):
    ws = await open_socket(rider)

    await ws.close()

    await eventually(lambda: rider["user"].id not in websocket_router.connections)


async def test_an_abrupt_close_removes_the_user(open_socket, rider):
    ws = await open_socket(rider)

    ws.transport.abort()  # no close frame

    await eventually(lambda: rider["user"].id not in websocket_router.connections)


async def test_the_oldest_socket_is_replaced_at_the_limit(open_socket, rider):
    user_id = rider["user"].id
    limit = websocket_router.MAX_CONNECTIONS_PER_USER
    sockets = [await open_socket(rider) for _ in range(limit + 1)]

    assert await close_code(sockets[0]) == 4409
    assert len(websocket_router.connections[user_id]) == limit
    await events.publish(user_id, "test", {"n": 1})
    for ws in sockets[1:]:
        assert (await receive(ws))["data"] == {"n": 1}


async def test_events_arrive_in_order_while_the_client_pings(open_socket, rider):
    ws = await open_socket(rider)
    pings = 50

    async def send_pings():
        for _ in range(pings):
            await ws.send(json.dumps({"type": "ping", "data": {}}))
            await asyncio.sleep(0)

    ping_task = asyncio.create_task(send_pings())
    for n in range(100):
        await events.publish(rider["user"].id, "seq", {"n": n})
    await ping_task

    frames = []
    while sum(f["type"] == "seq" for f in frames) < 100 or sum(f["type"] == "pong" for f in frames) < pings:
        frames.append(await receive(ws))
    assert [f["data"]["n"] for f in frames if f["type"] == "seq"] == list(range(100))
    assert sum(f["type"] == "pong" for f in frames) == pings
    assert all(f["type"] in ("seq", "pong") for f in frames)


async def test_bad_messages_on_the_channel_are_dropped(open_socket, rider):
    user_id = rider["user"].id
    ws = await open_socket(rider)
    bad = [
        "garbage",
        "[1, 2]",
        json.dumps({"type": "x", "data": {}}),
        json.dumps({"user_id": "abc", "type": "x", "data": {}}),
        json.dumps({"user_id": True, "type": "x", "data": {}}),
        json.dumps({"user_id": user_id, "type": 5, "data": {}}),
        json.dumps({"user_id": user_id, "type": "x"}),
    ]
    for raw in bad:
        await redis_client.publish(CHANNEL, raw)

    await redis_client.publish(CHANNEL, json.dumps({"user_id": user_id, "type": "good", "data": {"n": 1}}))

    # The first frame is the good one, so every bad message was dropped and the listener kept going.
    assert await receive(ws) == {"type": "good", "data": {"n": 1}}


async def test_the_listener_resubscribes_after_its_connection_is_killed(open_socket, rider, wait_for_listener):
    ws = await open_socket(rider)

    # Also drops the dev backend's listener, which reconnects by itself.
    killed = await redis_client.execute_command("CLIENT", "KILL", "TYPE", "pubsub")
    assert killed >= 1
    await wait_for_listener(1)

    await events.publish(rider["user"].id, "test", {"n": 1})
    assert (await receive(ws))["data"] == {"n": 1}
    assert ws.close_code is None


async def test_a_message_on_another_database_channel_is_not_delivered(open_socket, rider):
    ws = await open_socket(rider)

    await redis_client.publish(OTHER_DATABASE_CHANNEL, json.dumps({"user_id": rider["user"].id, "type": "x", "data": {}}))
    await assert_silent(ws)

    await events.publish(rider["user"].id, "test", {"n": 1})
    assert (await receive(ws))["type"] == "test"


async def test_a_client_that_stops_reading_is_dropped_and_delivery_continues(open_socket, live_server, rider, driver, monkeypatch):
    monkeypatch.setattr(websocket_router, "SEND_TIMEOUT_SECONDS", 1)
    # A hand-made client: a library client keeps reading in the background and the server never blocks.
    # This one has a tiny receive buffer and reads nothing after the handshake.
    host, port = urlparse(live_server).hostname, urlparse(live_server).port
    tiny = socket.socket()
    tiny.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
    tiny.connect((host, port))
    reader, writer = await asyncio.open_connection(sock=tiny)
    writer.write(
        b"GET /ws HTTP/1.1\r\nHost: test\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
        b"Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\nSec-WebSocket-Version: 13\r\n\r\n"
    )
    await reader.readuntil(b"\r\n\r\n")
    payload = auth_frame(token_of(rider)).encode()
    mask = b"\x01\x02\x03\x04"
    writer.write(bytes([0x81, 0x80 | 126]) + len(payload).to_bytes(2, "big") + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))
    try:
        await eventually(lambda: rider["user"].id in websocket_router.connections)
        healthy = await open_socket(driver)
        big = {"filler": "x" * 100_000}

        for _ in range(300):
            await events.publish(rider["user"].id, "big", big)
        started = time.monotonic()
        await events.publish(driver["user"].id, "small", {"n": 1})

        # The listener sends one message at a time, so the healthy user waits while it gives up on the stalled
        # one: up to SEND_TIMEOUT_SECONDS for the send and again for the close. Known limit, see PROJECT_CONTEXT.md.
        assert (await receive(healthy, timeout=5))["type"] == "small"
        assert time.monotonic() - started < 2 * websocket_router.SEND_TIMEOUT_SECONDS + 1
        await eventually(lambda: rider["user"].id not in websocket_router.connections, seconds=3)
    finally:
        writer.close()  # otherwise the server cannot shut down while its send to this client is stuck


async def test_auth_me_still_answers_401_for_bad_tokens(client, rider, make_user, db, expired_token, tampered_token):
    deleted = await make_user("rider")
    deleted_token = token_of(deleted)
    await db.delete(deleted["user"])
    await db.commit()

    response = await client.get("/auth/me")
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    for token in (tampered_token(rider["user"]), expired_token(rider["user"]), deleted_token, "garbage"):
        response = await client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})
        assert response.status_code == 401
        assert response.headers["www-authenticate"] == "Bearer"

    response = await client.get("/auth/me", headers=rider["headers"])
    assert response.status_code == 200
    assert response.json()["id"] == rider["user"].id
