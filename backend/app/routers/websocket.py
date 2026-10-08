import asyncio
import json
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from redis.exceptions import RedisError

from app import database
from app.repositories import events
from app.security import user_from_token

AUTH_TIMEOUT_SECONDS = 5
MAX_CONNECTIONS_PER_USER = 5
SEND_TIMEOUT_SECONDS = 5
RECONNECT_DELAY_SECONDS = 1

CLOSE_BAD_MESSAGE = 4400
CLOSE_UNAUTHORIZED = 4401
CLOSE_AUTH_TIMEOUT = 4408
CLOSE_REPLACED = 4409

# uvicorn's logger, because it is the one that has a handler and prints INFO.
logger = logging.getLogger("uvicorn.error")
router = APIRouter()

# user id -> that user's sockets on THIS process, oldest first. Only code that never awaits between
# reading and changing a list may touch it, so the lists cannot be corrupted by interleaving.
connections: dict[int, list[WebSocket]] = {}


@router.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket) -> None:
    # The token is in the first message, never in the URL: URLs end up in logs and history,
    # and browsers cannot set headers on a WebSocket.
    await websocket.accept()
    try:
        frame = await asyncio.wait_for(websocket.receive(), AUTH_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        await websocket.close(code=CLOSE_AUTH_TIMEOUT)
        return
    if frame["type"] == "websocket.disconnect":
        return

    try:
        first = json.loads(frame["text"]) if frame.get("text") is not None else None
    except ValueError:
        first = None
    data = first.get("data") if isinstance(first, dict) else None
    token = data.get("token") if isinstance(data, dict) else None
    if not isinstance(first, dict) or first.get("type") != "auth" or not isinstance(token, str):
        await websocket.close(code=CLOSE_UNAUTHORIZED)
        return

    # A short session just for this lookup. Depends(get_db) would hold a Postgres connection as long as the socket lives.
    async with database.async_session() as db:
        user = await user_from_token(db, token)
    if user is None:
        await websocket.close(code=CLOSE_UNAUTHORIZED)
        return
    user_id, role = user.id, user.role.value

    # No await between the count check and the append. At the limit the newest wins, so a page reload
    # is never locked out by a dead socket.
    sockets = connections.setdefault(user_id, [])
    oldest = sockets.pop(0) if len(sockets) >= MAX_CONNECTIONS_PER_USER else None
    sockets.append(websocket)

    try:
        await websocket.send_json({"type": "auth_ok", "data": {"user_id": user_id, "role": role}})
        logger.info("WebSocket connected: user %s (%s), %s connection(s)", user_id, role, len(sockets))
        if oldest is not None:
            try:
                await asyncio.wait_for(oldest.close(code=CLOSE_REPLACED), SEND_TIMEOUT_SECONDS)
            except Exception:
                pass  # the old socket is probably dead already; it is out of the registry either way

        while True:
            frame = await websocket.receive()
            if frame["type"] == "websocket.disconnect":
                break
            try:
                message = json.loads(frame["text"]) if frame.get("text") is not None else None
            except ValueError:
                message = None
            if not isinstance(message, dict) or not isinstance(message.get("type"), str):
                await websocket.close(code=CLOSE_BAD_MESSAGE)
                break
            if message["type"] == "ping":
                await websocket.send_json({"type": "pong", "data": {}})
            else:
                await websocket.send_json({"type": "error", "data": {"detail": f"Unknown message type: {message['type'][:50]}"}})
    except WebSocketDisconnect:
        pass  # a send to a client that has just gone away
    finally:
        # The socket may already be gone from the list (replaced by a newer connection, or dropped by the listener).
        remaining = connections.get(user_id, [])
        if websocket in remaining:
            remaining.remove(websocket)
        if not remaining:
            connections.pop(user_id, None)
        logger.info("WebSocket disconnected: user %s (%s), %s connection(s)", user_id, role, len(remaining))


async def listen_for_events() -> None:
    """Runs for the life of the process: forwards every event on the Redis channel to the addressed user's local sockets."""
    is_down = False
    while True:
        pubsub = None
        try:
            pubsub = await events.subscribe()
            if is_down:
                logger.info("WebSocket event listener recovered")
                is_down = False
            async for message in pubsub.listen():
                try:
                    event = json.loads(message["data"])
                except ValueError:
                    event = None
                if (
                    not isinstance(event, dict)
                    or type(event.get("user_id")) is not int
                    or not isinstance(event.get("type"), str)
                    or not isinstance(event.get("data"), dict)
                ):
                    logger.warning("WebSocket event listener dropped a malformed message")
                    continue

                # A copy, because a failed send removes the socket from the list.
                for socket in list(connections.get(event["user_id"], [])):
                    try:
                        await asyncio.wait_for(
                            socket.send_json({"type": event["type"], "data": event["data"]}), SEND_TIMEOUT_SECONDS
                        )
                    except Exception:
                        # Failed or too slow: drop this socket. The endpoint's finally block logs the disconnect,
                        # which for a stalled client only happens once its connection really ends.
                        logger.warning("WebSocket send to user %s failed or timed out, dropping that socket", event["user_id"])
                        remaining = connections.get(event["user_id"], [])
                        if socket in remaining:
                            remaining.remove(socket)
                        if not remaining:
                            connections.pop(event["user_id"], None)
                        try:
                            await asyncio.wait_for(socket.close(), SEND_TIMEOUT_SECONDS)
                        except Exception:
                            pass
        except RedisError as error:
            if not is_down:
                logger.warning("WebSocket event listener is down, retrying every %s s: %s", RECONNECT_DELAY_SECONDS, error)
                is_down = True
        finally:
            if pubsub is not None:
                await pubsub.aclose()
        await asyncio.sleep(RECONNECT_DELAY_SECONDS)
