import logging
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP

from fastapi import HTTPException
from redis.exceptions import RedisError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ACTIVE_RIDE_STATUSES, OfferStatus, RideStatus
from app.repositories import admin as admin_repo
from app.repositories import drivers as drivers_repo
from app.repositories import stats as stats_repo
from app.services import earnings

HOUR_MAX_DAYS = 14
DAY_MAX_DAYS = 366
OFFSET_MAX_MINUTES = 840

# uvicorn's logger, because it is the one that has a handler and prints INFO.
logger = logging.getLogger("uvicorn.error")


async def get_stats(db: AsyncSession, since: datetime, until: datetime, bucket: str, utc_offset_minutes: int) -> dict:
    """Two cohorts. Ride counts and trip averages are by REQUEST time (rides.created_at); money is by SETTLEMENT time
    (ride_earnings.created_at, as in the earnings views), so the two can differ for rides that cross the window edge.
    Buckets use a fixed UTC offset sent by the browser, with plain arithmetic and no timezone database. Rates are percent with
    one decimal, rounded half up in integers. The queries are not one atomic snapshot."""
    if until <= since:
        raise HTTPException(status_code=422, detail="until must be after since")
    max_days = HOUR_MAX_DAYS if bucket == "hour" else DAY_MAX_DAYS
    if until - since > timedelta(days=max_days):
        raise HTTPException(status_code=422, detail=f"The window can be at most {max_days} days with {bucket} buckets")
    if abs(utc_offset_minutes) > OFFSET_MAX_MINUTES:
        raise HTTPException(status_code=422, detail=f"utc_offset_minutes must be between -{OFFSET_MAX_MINUTES} and {OFFSET_MAX_MINUTES}")

    bucket_seconds = 3600 if bucket == "hour" else 86400
    offset_seconds = utc_offset_minutes * 60

    by_status = await stats_repo.count_rides_by_status(db, since, until)
    completed = by_status.get(RideStatus.COMPLETED, 0)
    cancelled = by_status.get(RideStatus.CANCELLED, 0)
    no_driver_found = by_status.get(RideStatus.NO_DRIVER_FOUND, 0)
    finished = completed + cancelled + no_driver_found  # the denominator of the rates: active rides have no outcome yet
    rides = {
        "requested": sum(by_status.values()),
        "completed": completed,
        "cancelled": cancelled,
        "no_driver_found": no_driver_found,
        "active": sum(by_status.get(status, 0) for status in ACTIVE_RIDE_STATUSES),
        "completion_rate": None if finished == 0 else (completed * 2000 + finished) // (2 * finished) / 10,
        "cancellation_rate": None if finished == 0 else (cancelled * 2000 + finished) // (2 * finished) / 10,
        "no_driver_rate": None if finished == 0 else (no_driver_found * 2000 + finished) // (2 * finished) / 10,
    }

    sums = await stats_repo.trip_averages(db, since, until)
    wait = await stats_repo.assignment_times(db, since, until)
    trips = {
        "avg_fare": None if sums.fares == 0 else (sums.fare_total * 2 + sums.fares) // (2 * sums.fares),
        "avg_distance_m": None if sums.distances == 0 else (sums.distance_total * 2 + sums.distances) // (2 * sums.distances),
        "avg_duration_s": None if sums.durations == 0 else (sums.duration_total * 2 + sums.durations) // (2 * sums.durations),
        "assigned_rides": wait.rides,
        "mean_time_to_assign_s": None if wait.mean_s is None else float((wait.mean_s * 10).to_integral_value(rounding=ROUND_HALF_UP) / 10),
        "median_time_to_assign_s": None if wait.median_s is None else float((wait.median_s * 10).to_integral_value(rounding=ROUND_HALF_UP) / 10),
    }

    offer_counts = await stats_repo.count_offers_by_status(db, since, until)
    answered = sum(offer_counts.get(status, 0) for status in (OfferStatus.ACCEPTED, OfferStatus.REJECTED, OfferStatus.EXPIRED))
    accepted = offer_counts.get(OfferStatus.ACCEPTED, 0)
    offers = {
        **{status.value.lower(): offer_counts.get(status, 0) for status in OfferStatus},
        "acceptance_rate": None if answered == 0 else (accepted * 2000 + answered) // (2 * answered) / 10,
    }

    surged = await stats_repo.surge_summary(db, since, until)
    new_users = await stats_repo.count_new_users(db, since, until)

    # Zero-filled here, so a quiet hour is a bar of height 0 and not a gap. Integer seconds: since rounds down, until up.
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    since_s = (since - epoch) // timedelta(seconds=1)
    until_s = -((epoch - until) // timedelta(seconds=1))
    first = (since_s + offset_seconds) // bucket_seconds
    last = (until_s - 1 + offset_seconds) // bucket_seconds
    ride_buckets = await stats_repo.ride_series(db, since, until, bucket_seconds, offset_seconds)
    money_buckets = await stats_repo.money_series(db, since, until, bucket_seconds, offset_seconds)
    series = [
        {
            "start": index * bucket_seconds - offset_seconds,
            "rides": ride_buckets.get(index, (0, 0))[0],
            "completed": ride_buckets.get(index, (0, 0))[1],
            "gross": money_buckets.get(index, (0, 0))[0],
            "platform_fee": money_buckets.get(index, (0, 0))[1],
        }
        for index in range(first, last + 1)
    ]

    try:
        online_drivers = len(await drivers_repo.get_online_positions())
    except RedisError:
        # The stats must stay available when Redis is down: only this one number is missing.
        logger.warning("Could not count the online drivers for the stats: Redis is unavailable")
        online_drivers = None
    active = await admin_repo.count_active_rides_by_status(db)

    return {
        "since": since,
        "until": until,
        "bucket": bucket,
        "utc_offset_minutes": utc_offset_minutes,
        "rides": rides,
        "trips": trips,
        "offers": offers,
        "surge": {"rides_surged": surged.rides_surged, "max_surge_percent": surged.max_surge_percent or 100},
        "money": await earnings.get_revenue(db, since, until),
        "users": {"new_riders": new_users.get("rider", 0), "new_drivers": new_users.get("driver", 0)},
        "now": {
            "online_drivers": online_drivers,
            "active_rides": {status.value: active.get(status.value, 0) for status in ACTIVE_RIDE_STATUSES},
            "pending_offers": await stats_repo.count_pending_offers(db),
        },
        "series": series,
    }
