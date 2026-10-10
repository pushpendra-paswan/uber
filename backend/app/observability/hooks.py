import asyncio
import contextvars
import logging

from sqlalchemy import event
from sqlalchemy.orm import Session, object_session

from app import database
from app.models import ACTIVE_RIDE_STATUSES, RideEvent
from app.observability import metrics
from app.observability.context import request_context
from app.repositories import admin as admin_repo
from app.repositories import drivers as drivers_repo
from app.repositories import stats as stats_repo

METRICS_REFRESH_SECONDS = 5
LAG_SLEEP_SECONDS = 0.1

logger = logging.getLogger("app.observability")


def start_background_task(component: str, coroutine) -> asyncio.Task:
    """Starts a task whose log lines carry `component` and no request id. The context is set once, here."""
    context = contextvars.copy_context()
    context.run(request_context.set, {"component": component})
    return asyncio.create_task(coroutine, context=context)


# Ride transitions are counted and logged only after the transaction that wrote their ride_events row has committed.
# session.info["pending_transitions"] holds (transaction, ride_id, from_status, to_status), where the transaction is the
# savepoint the event was written in, or else the outermost transaction.
@event.listens_for(RideEvent, "after_insert")
def note_transition(mapper, connection, event_row: RideEvent) -> None:
    session = object_session(event_row)
    transaction = session.get_nested_transaction() or session.get_transaction()
    session.info.setdefault("pending_transitions", []).append(
        (transaction, event_row.ride_id, event_row.from_status, event_row.to_status)
    )


@event.listens_for(Session, "after_soft_rollback")
def drop_rolled_back_transitions(session: Session, previous_transaction) -> None:
    # A savepoint rollback drops only what was written inside that savepoint; the rest waits for the outer commit.
    if previous_transaction.nested:
        session.info["pending_transitions"] = [
            item for item in session.info.get("pending_transitions", []) if item[0] is not previous_transaction
        ]


@event.listens_for(Session, "after_commit")
def emit_transitions(session: Session) -> None:
    # Fires for the release of a savepoint too. Only the outermost commit makes the events durable.
    if session.in_nested_transaction():
        return
    for _, ride_id, from_status, to_status in session.info.get("pending_transitions", []):
        from_name = from_status.value if from_status is not None else "none"
        try:
            metrics.ride_transitions_total.labels(from_name, to_status.value).inc()
            logger.info("ride_transition", extra={"ride_id": ride_id, "from_status": from_name, "to_status": to_status.value})
        except Exception:
            # Runs after the commit: a failure here must not fail a request whose work is already saved.
            metrics.observability_errors_total.labels("hook").inc()
            logger.error("observability_hook_error", exc_info=True)


@event.listens_for(Session, "after_transaction_end")
def clear_pending_transitions(session: Session, transaction) -> None:
    # The end of the outermost transaction (commit, rollback, or close) leaves nothing pending, so a reused session starts clean.
    if transaction.parent is None:
        session.info.pop("pending_transitions", None)


async def refresh_gauges_forever() -> None:
    """Runs for the life of the process. Database state is read here, every METRICS_REFRESH_SECONDS, instead of when
    Prometheus scrapes, so a scrape costs nothing and a slow database cannot slow it down."""
    while True:
        try:
            async with database.async_session() as db:
                active = await admin_repo.count_active_rides_by_status(db)
                pending = await stats_repo.count_pending_offers(db)
            for status in ACTIVE_RIDE_STATUSES:
                metrics.rides_active.labels(status.value).set(active.get(status.value, 0))
            metrics.offers_pending.set(pending)
            pool = database.engine.pool
            metrics.db_pool_size.set(pool.size())
            metrics.db_pool_checked_out.set(pool.checkedout())
            metrics.db_pool_overflow.set(pool.overflow())
        except Exception:
            metrics.observability_errors_total.labels("gauges_db").inc()
            logger.error("gauges_error", exc_info=True, extra={"service": "postgres"})

        try:
            metrics.drivers_online.set(len(await drivers_repo.get_online_positions()))
        except Exception:
            metrics.observability_errors_total.labels("gauges_redis").inc()
            logger.error("gauges_error", exc_info=True, extra={"service": "redis"})

        await asyncio.sleep(METRICS_REFRESH_SECONDS)


async def loop_lag_monitor() -> None:
    """Runs for the life of the process. A sleep of LAG_SLEEP_SECONDS that wakes up later than that shows how long the event
    loop was busy with something else (a blocking call, or more ready callbacks than it could run)."""
    loop = asyncio.get_running_loop()
    failing = False
    while True:
        try:
            started = loop.time()
            await asyncio.sleep(LAG_SLEEP_SECONDS)
            metrics.event_loop_lag_seconds.observe(max(0.0, loop.time() - started - LAG_SLEEP_SECONDS))
            failing = False
        except Exception:
            metrics.observability_errors_total.labels("hook").inc()
            if not failing:  # one line per streak of failures, not ten per second
                logger.error("loop_lag_error", exc_info=True)
                failing = True
