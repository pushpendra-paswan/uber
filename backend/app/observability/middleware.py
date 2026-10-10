import logging
import re
import sys
import time
import uuid

from starlette.staticfiles import StaticFiles

from app.observability import metrics
from app.observability.context import request_context

REQUEST_ID_PATTERN = re.compile(r"[A-Za-z0-9._-]{8,64}")
KNOWN_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS")
QUIET_ROUTES = ("/health", "/metrics")  # polled constantly: DEBUG, not INFO

logger = logging.getLogger("app.access")


def observability_middleware(app):
    """Pure ASGI middleware (not BaseHTTPMiddleware, which runs the app in another task and hides the context).
    A function that returns the ASGI callable, so there is no class."""

    async def middleware(scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            await app(scope, receive, send)
            return

        # The inbound id is only kept when it is plain and short: it ends up in every log line.
        inbound = next((value.decode("latin-1") for name, value in scope["headers"] if name == b"x-request-id"), "")
        request_id = inbound if REQUEST_ID_PATTERN.fullmatch(inbound) else uuid.uuid4().hex
        context = {"request_id": request_id, "user_id": None, "role": None, "db_queries": 0, "db_ms": 0.0}
        # Not reset afterwards: uvicorn logs an unhandled exception after this function has returned, in the same task.
        request_context.set(context)

        if scope["type"] == "websocket":
            await app(scope, receive, send)
            return

        status = 500
        response_started = False
        error = None

        async def send_with_request_id(message):
            nonlocal status, response_started
            if message["type"] == "http.response.start":
                status = message["status"]
                response_started = True
                message = {**message, "headers": [*message.get("headers", []), (b"x-request-id", request_id.encode())]}
            await send(message)

        metrics.http_requests_in_flight.inc()
        started = time.perf_counter()
        try:
            await app(scope, receive, send_with_request_id)
        except Exception:
            if response_started:
                raise  # nothing can be sent any more: uvicorn logs this one
            # The access line below carries the type and stack (never the message): it is the one ERROR line of this request.
            # The response is sent here instead of by Starlette's outer error middleware, so that it carries the request id
            # header and uvicorn has nothing to log again.
            error = sys.exc_info()
            await send_with_request_id({"type": "http.response.start", "status": 500, "headers": [(b"content-type", b"text/plain; charset=utf-8")]})
            await send_with_request_id({"type": "http.response.body", "body": b"Internal Server Error"})
        finally:
            metrics.http_requests_in_flight.dec()
            try:
                seconds = time.perf_counter() - started
                # The route template, never the path. The frontend mount matches every path, so its 404s are unmatched ones.
                route = scope.get("route")
                if route is not None:
                    template = route.path
                elif isinstance(scope.get("endpoint"), StaticFiles) and status != 404:
                    template = "static"
                else:
                    template = "unmatched"
                method = scope["method"] if scope["method"] in KNOWN_METHODS else "OTHER"
                metrics.http_requests_total.labels(method, template, str(status)).inc()
                metrics.http_request_duration_seconds.labels(method, template).observe(seconds)

                line = {"method": method, "route": template, "status": status, "duration_ms": round(seconds * 1000, 1),
                        "db_queries": context["db_queries"], "db_ms": round(context["db_ms"], 1)}
                ride_id = scope.get("path_params", {}).get("ride_id")
                if isinstance(ride_id, str) and ride_id.isascii() and ride_id.isdigit():
                    line["ride_id"] = int(ride_id)
                level = logging.ERROR if status >= 500 else logging.DEBUG if template in QUIET_ROUTES else logging.INFO
                logger.log(level, "http_request", extra=line, exc_info=error)
            except Exception:
                metrics.observability_errors_total.labels("middleware").inc()
                logging.getLogger("app.observability").error("observability_middleware_error", exc_info=True)

    return middleware
