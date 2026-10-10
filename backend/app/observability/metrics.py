import time
from contextlib import contextmanager

import httpx
from prometheus_client import Counter, Gauge, Histogram
from sqlalchemy import event
from sqlalchemy.engine import Engine

from app.observability.context import request_context

# Label values come only from fixed sets or route templates: never an id, a path, an email, or a message.
http_requests_total = Counter("http_requests_total", "Finished HTTP requests", ["method", "route", "status"])
http_request_duration_seconds = Histogram(
    "http_request_duration_seconds", "HTTP request duration", ["method", "route"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10),
)
http_requests_in_flight = Gauge("http_requests_in_flight", "HTTP requests currently running")
db_query_duration_seconds = Histogram(
    "db_query_duration_seconds", "Duration of one SQL statement",
    buckets=(0.001, 0.0025, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5),
)
db_pool_size = Gauge("db_pool_size", "Connections the pool keeps")
db_pool_checked_out = Gauge("db_pool_checked_out", "Connections in use")
db_pool_overflow = Gauge("db_pool_overflow", "Pool overflow counter (connections created minus the pool size)")
external_call_duration_seconds = Histogram(
    "external_call_duration_seconds", "Duration of a call to OSRM, Nominatim, or Stripe", ["service", "outcome"],
    buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10),
)
ws_connections = Gauge("ws_connections", "Accepted WebSockets not yet closed")
ws_closes_total = Counter("ws_closes_total", "Closed WebSockets", ["code"])
ride_transitions_total = Counter("ride_transitions_total", "Committed ride events", ["from_status", "to_status"])
rides_active = Gauge("rides_active", "Rides in an active status", ["status"])
offers_pending = Gauge("offers_pending", "Pending offers, expired ones the sweeper has not handled included")
drivers_online = Gauge("drivers_online", "Drivers with a presence key")
sweeper_tick_duration_seconds = Histogram(
    "sweeper_tick_duration_seconds", "One pass of the offers sweeper", buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5)
)
sweeper_last_success_timestamp_seconds = Gauge("sweeper_last_success_timestamp_seconds", "Epoch seconds of the last sweeper pass that did not fail")
sweeper_errors_total = Counter("sweeper_errors_total", "Failed sweeper passes")
observability_errors_total = Counter("observability_errors_total", "Failures of the observability code itself", ["source"])


@contextmanager
def timed_external(service: str):
    """Times a call to OSRM, Nominatim, or Stripe. An exception passes through unchanged; only the outcome label differs."""
    started = time.perf_counter()
    outcome = "ok"
    try:
        yield
    except httpx.TimeoutException:
        outcome = "timeout"
        raise
    except BaseException:
        outcome = "error"
        raise
    finally:
        external_call_duration_seconds.labels(service, outcome).observe(time.perf_counter() - started)


# Registered on the Engine CLASS, so the tests' own engine is timed too. The statement and its parameters are never read.
@event.listens_for(Engine, "before_cursor_execute")
def start_query_timer(connection, cursor, statement, parameters, context, executemany):
    connection.info["query_started"] = time.perf_counter()


@event.listens_for(Engine, "after_cursor_execute")
def end_query_timer(connection, cursor, statement, parameters, context, executemany):
    started = connection.info.pop("query_started", None)
    if started is None:  # the listener was attached while this statement was already running
        return
    seconds = time.perf_counter() - started
    db_query_duration_seconds.observe(seconds)
    # Only a request has these two keys: a background task has none, so its counters never pile up.
    request = request_context.get(None)
    if request is not None and "db_queries" in request:
        request["db_queries"] += 1
        request["db_ms"] += seconds * 1000
