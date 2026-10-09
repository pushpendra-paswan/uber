import logging
import math
import time
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException
from redis.exceptions import RedisError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Ride, RideStatus, UserRole
from app.repositories import drivers as drivers_repo
from app.repositories import pricing as pricing_repo
from app.repositories import rides as rides_repo
from app.utils.geo import geohash_encode

FARE_CAP_PERCENT = 150  # the rider never pays more than this share of the estimate
MAX_PLAUSIBLE_SPEED_MS = 45  # 162 km/h: a faster move between two pings is a jump, not driving
MIN_PING_GAP_S = 1  # pings closer together than this are ignored
UNRELIABLE_BELOW_PERCENT = 50  # with jumps seen, tracked distance below this share of the estimate is not trusted
TRIP_TTL_SECONDS = 86400
EARTH_RADIUS_M = 6371000

# Surge (M5.2). Multipliers are integer percents: 100 is no surge, 150 is 1.5x.
ZONE_PRECISION = 5  # geohash length of a zone: cells of about 4.9 km by 3.8 km
DEMAND_WINDOW_S = 180  # a rider whose request found no driver counts as demand for this long
MIN_DEMAND_FOR_SURGE = 3  # fewer unmet riders in a zone never cause surge
# "Pressure (demand per available driver, in percent) up to this value gives this multiplier percent."
SURGE_STEPS = ((100, 100), (150, 120), (200, 150), (300, 180))
SURGE_MAX_PERCENT = 200  # above the last step
SURGE_CACHE_TTL_S = 15  # 0 or less turns the snapshot cache off

# uvicorn's logger, because it is the one that has a handler and prints INFO.
logger = logging.getLogger("uvicorn.error")


def fare_cap(fare_estimate: int) -> int:
    """The most a trip can cost, in paise. Settlement, the estimate, the wallet reservation and the wallet check at request
    all use it, so they cannot disagree."""
    return fare_estimate * FARE_CAP_PERCENT // 100


async def get_surge_snapshot(db: AsyncSession, force: bool = False) -> dict:
    """One city-wide snapshot of demand, supply and the step multiplier of every zone, shared through Redis so everyone
    asking within SURGE_CACHE_TTL_S sees the same numbers. Raises RedisError when Redis fails."""
    if not force and SURGE_CACHE_TTL_S > 0:
        cached = await pricing_repo.get_snapshot()
        if cached is not None:
            return cached

    started = datetime.now(timezone.utc)
    demand = await rides_repo.count_unmet_demand_by_zone(db, started - timedelta(seconds=DEMAND_WINDOW_S))
    positions = await drivers_repo.get_online_positions()
    available_ids = await drivers_repo.get_available_ids(db, list(positions)) if positions else set()

    supply = {}
    for driver_id in available_ids:
        lat, lng = positions[driver_id]
        zone = geohash_encode(lat, lng, ZONE_PRECISION)
        supply[zone] = supply.get(zone, 0) + 1

    zones = {}
    for zone in set(demand) | set(supply):
        zone_demand = demand.get(zone, 0)
        zone_supply = supply.get(zone, 0)
        pressure_percent = zone_demand * 100 // max(zone_supply, 1)
        surge_percent = 100
        if zone_demand >= MIN_DEMAND_FOR_SURGE:
            surge_percent = SURGE_MAX_PERCENT
            for up_to, step_percent in SURGE_STEPS:
                if pressure_percent <= up_to:
                    surge_percent = step_percent
                    break
        zones[zone] = {"demand": zone_demand, "supply": zone_supply, "pressure_percent": pressure_percent, "surge_percent": surge_percent}

    snapshot = {"computed_at": int(started.timestamp()), "zones": zones}
    if SURGE_CACHE_TTL_S > 0:
        await pricing_repo.save_snapshot(snapshot, SURGE_CACHE_TTL_S)
    milliseconds = (datetime.now(timezone.utc) - started).total_seconds() * 1000
    logger.info(
        "Surge snapshot computed: %d zones, %d above 1.0x, %.0f ms",
        len(zones), sum(1 for zone in zones.values() if zone["surge_percent"] > 100), milliseconds,
    )
    return snapshot


async def get_surge_percent(db: AsyncSession, lat: float, lng: float, vehicle_type: str = "economy") -> tuple[str, int]:
    """The zone of a pickup and its multiplier in percent, capped by the pricing rule. Redis trouble means no surge:
    a quote must not fail because the snapshot cannot be read."""
    zone = geohash_encode(lat, lng, ZONE_PRECISION)
    try:
        snapshot = await get_surge_snapshot(db)
    except RedisError:
        logger.warning("Could not read the surge snapshot: quoting without surge")
        snapshot = None

    rule = await pricing_repo.get_rule(db, vehicle_type)
    if rule is None:
        raise HTTPException(status_code=503, detail="Pricing is not configured")
    if snapshot is None or zone not in snapshot["zones"]:
        return zone, 100
    # The cap is read from the rule now, not stored in the snapshot, so a changed rule takes effect at once.
    cap_percent = max(100, int(rule.surge_cap * 100 + 0.5))
    return zone, min(snapshot["zones"][zone]["surge_percent"], cap_percent)


async def get_surge_zones(db: AsyncSession, force: bool) -> dict:
    """The snapshot as the admin endpoint shows it: the busiest zones first."""
    snapshot = await get_surge_snapshot(db, force)
    zones = [{"zone": zone, **numbers} for zone, numbers in snapshot["zones"].items()]
    zones.sort(key=lambda zone: (-zone["surge_percent"], -zone["demand"], zone["zone"]))
    age_seconds = max(0, int(datetime.now(timezone.utc).timestamp()) - snapshot["computed_at"])
    return {"computed_at": snapshot["computed_at"], "age_seconds": age_seconds, "zones": zones}


async def calculate_fare(
    db: AsyncSession, distance_m: int, duration_s: int, surge_percent: int = 100, vehicle_type: str = "economy"
) -> dict:
    rule = await pricing_repo.get_rule(db, vehicle_type)
    if rule is None:
        raise HTTPException(status_code=503, detail="Pricing is not configured")

    # Integer paise only. Adding half the divisor before // rounds half up.
    distance_fare = (rule.per_km * distance_m + 500) // 1000
    time_fare = (rule.per_min * duration_s + 30) // 60
    subtotal = rule.base_fare + distance_fare + time_fare
    normal_fare = max(subtotal, rule.min_fare)
    # Surge multiplies the whole normal fare, the minimum fare included.
    fare = (normal_fare * surge_percent + 50) // 100
    return {
        "base_fare": rule.base_fare,
        "distance_fare": distance_fare,
        "time_fare": time_fare,
        "fare_estimate": fare,
        "minimum_fare_applied": rule.min_fare > subtotal,
        "normal_fare": normal_fare,
        "surge_percent": surge_percent,
        "surge_amount": fare - normal_fare,
    }


async def record_trip_point(ride_id: int, lat: float, lng: float) -> None:
    """Adds one driver ping of an IN_PROGRESS ride to the distance meter in Redis (there is no location history).
    Metering is secondary, so a Redis failure is logged and never fails the ping."""
    now = time.time()
    try:
        trip = await rides_repo.get_trip(ride_id)
        if trip is None:
            trip = {"distance_m": 0, "lat": lat, "lng": lng, "ts": now, "pings": 1, "jumps": 0}
        elif now - trip["ts"] < MIN_PING_GAP_S:
            return
        else:
            # Straight line between the two pings (haversine).
            phi_1, phi_2 = math.radians(trip["lat"]), math.radians(lat)
            half_chord = (
                math.sin((phi_2 - phi_1) / 2) ** 2
                + math.cos(phi_1) * math.cos(phi_2) * math.sin(math.radians(lng - trip["lng"]) / 2) ** 2
            )
            segment_m = 2 * EARTH_RADIUS_M * math.asin(math.sqrt(half_chord))
            if segment_m / (now - trip["ts"]) > MAX_PLAUSIBLE_SPEED_MS:
                trip["jumps"] += 1
            else:
                trip["distance_m"] += segment_m
            trip.update(lat=lat, lng=lng, ts=now, pings=trip["pings"] + 1)
        await rides_repo.save_trip(ride_id, trip, TRIP_TTL_SECONDS)
    except RedisError:
        logger.warning("Could not record a trip point for ride %s: the distance meter is unavailable", ride_id)


async def settle_completed_ride(db: AsyncSession, ride: Ride) -> None:
    """Sets the final fare of a ride that was just completed. Does not commit: the caller's transaction holds the ride lock."""
    duration_s = max(1, int((ride.completed_at - ride.started_at).total_seconds() + 0.5))  # half up

    try:
        trip = await rides_repo.get_trip(ride.id)
    except RedisError:
        logger.warning("Could not read the distance meter of ride %s: billing the estimated distance", ride.id)
        trip = None

    # The estimated distance is the one the rider agreed to, so it is billed whenever tracking cannot be trusted.
    fallback_reason = None
    if trip is None or trip["pings"] < 2:
        fallback_reason = "no_tracking"
    elif trip["jumps"] > 0 and trip["distance_m"] * 100 < ride.distance_m * UNRELIABLE_BELOW_PERCENT:
        fallback_reason = "unreliable_tracking"
    distance_m = ride.distance_m if fallback_reason else int(trip["distance_m"] + 0.5)

    # The multiplier locked on the ride at request time, never the current one.
    fare = await calculate_fare(db, distance_m, duration_s, ride.surge_percent)
    cap = fare_cap(ride.fare_estimate)
    ride.final_fare = min(fare["fare_estimate"], cap)
    ride.actual_distance_m = distance_m
    ride.actual_duration_s = duration_s
    ride.fare_breakdown = {
        "kind": "trip",
        "distance_m": distance_m,
        "duration_s": duration_s,
        "distance_source": "estimate" if fallback_reason else "tracked",
        "fallback_reason": fallback_reason,
        "tracked_pings": trip["pings"] if trip else 0,
        "jumps_ignored": trip["jumps"] if trip else 0,
        "base_fare": fare["base_fare"],
        "distance_fare": fare["distance_fare"],
        "time_fare": fare["time_fare"],
        "minimum_fare_applied": fare["minimum_fare_applied"],
        "computed_fare": fare["fare_estimate"],
        "normal_fare": fare["normal_fare"],
        "surge_percent": fare["surge_percent"],
        "surge_amount": fare["surge_amount"],
        "fare_cap": cap,
        "capped": fare["fare_estimate"] > cap,
    }


async def cancellation_fee(db: AsyncSession, ride: Ride, status: RideStatus, role: UserRole) -> tuple[int, str]:
    """The fee and the reason for a cancel by `role` from `status` (the ride's status BEFORE the cancel).
    The quote endpoint and the real cancel both call this, so they cannot disagree."""
    if role == UserRole.driver:
        return 0, "driver_cancelled"
    if status == RideStatus.REQUESTED:
        return 0, "no_driver_yet"

    rule = await pricing_repo.get_rule(db, "economy")
    if rule is None:
        raise HTTPException(status_code=503, detail="Pricing is not configured")
    if status == RideStatus.DRIVER_ARRIVED:
        return rule.cancellation_fee, "driver_arrived"
    assigned_at = await rides_repo.get_last_event_time(db, ride.id, RideStatus.DRIVER_ASSIGNED)
    if (datetime.now(timezone.utc) - assigned_at).total_seconds() <= rule.free_cancel_seconds:
        return 0, "within_free_window"
    return rule.cancellation_fee, "late_cancellation"


async def settle_cancelled_ride(db: AsyncSession, ride: Ride, previous_status: RideStatus, role: UserRole) -> None:
    """Sets the cancellation fee (which may be 0) of a ride that was just cancelled. Does not commit."""
    fee, reason = await cancellation_fee(db, ride, previous_status, role)
    ride.final_fare = fee
    ride.fare_breakdown = {"kind": "cancellation", "fee": fee, "reason": reason, "cancelled_by": role.value}
