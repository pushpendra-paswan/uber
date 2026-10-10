from datetime import datetime

from sqlalchemy import exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.models import (
    ACTIVE_RIDE_STATUSES, Driver, OfferStatus, Payment, Rating, RatingSummary, Ride, RideEarning, RideOffer, RideStatus, User,
    VerificationStatus, Vehicle,
)
from app.repositories.drivers import LOAD_DRIVER

# The statuses in which a driver is busy with a ride (REQUESTED has no driver yet).
ON_RIDE_STATUSES = (RideStatus.DRIVER_ASSIGNED, RideStatus.DRIVER_ARRIVED, RideStatus.IN_PROGRESS)

# Every ride column except the trip code: admin queries select this list, never the entity, so the code cannot leak.
RIDE_COLUMNS = [column for column in Ride.__table__.c if column.name != "otp"]

# Read-only queries for the admin pages (M6.2). They take no locks, flush nothing and commit nothing.


async def get_driver_states(db: AsyncSession, driver_ids: list[int]) -> dict[int, dict]:
    """ONE query: {driver_id: {"active_ride_id": id or None, "has_pending_offer": bool}}. uq_rides_one_active_per_driver
    means at most one ride matches."""
    if not driver_ids:
        return {}
    active_ride_id = (
        select(Ride.id).where(Ride.driver_id == Driver.id, Ride.status.in_(ON_RIDE_STATUSES)).limit(1).scalar_subquery()
    )
    has_pending_offer = exists().where(RideOffer.driver_id == Driver.id, RideOffer.status == OfferStatus.PENDING)
    result = await db.execute(
        select(Driver.id, active_ride_id.label("active_ride_id"), has_pending_offer.label("has_pending_offer"))
        .where(Driver.id.in_(driver_ids))
    )
    return {row.id: {"active_ride_id": row.active_ride_id, "has_pending_offer": row.has_pending_offer} for row in result.all()}


async def list_live_drivers(db: AsyncSession, driver_ids: list[int]) -> list[dict]:
    """id, name and plate of the given drivers. An id without a driver row (a stale Redis member) is skipped."""
    if not driver_ids:
        return []
    result = await db.execute(
        select(Driver.id, User.name, Vehicle.plate_number)
        .join(User, User.id == Driver.user_id)
        .outerjoin(Vehicle, Vehicle.driver_id == Driver.id)
        .where(Driver.id.in_(driver_ids))
        .order_by(Driver.id)
    )
    return [dict(row) for row in result.mappings().all()]


async def list_active_rides(db: AsyncSession, limit: int) -> list[dict]:
    """Newest first, up to limit + 1 rows: the extra row tells the caller that the list was cut."""
    result = await db.execute(
        select(
            Ride.id, Ride.status, Ride.pickup_lat, Ride.pickup_lng, Ride.pickup_address, Ride.dropoff_lat, Ride.dropoff_lng,
            Ride.dropoff_address, Ride.driver_id, Ride.rider_id, User.name.label("rider_name"), Ride.created_at, Ride.fare_estimate,
        )
        .join(User, User.id == Ride.rider_id)
        .where(Ride.status.in_(ACTIVE_RIDE_STATUSES))
        .order_by(Ride.id.desc())
        .limit(limit + 1)
    )
    return [dict(row) for row in result.mappings().all()]


async def count_active_rides_by_status(db: AsyncSession) -> dict[str, int]:
    result = await db.execute(select(Ride.status, func.count()).where(Ride.status.in_(ACTIVE_RIDE_STATUSES)).group_by(Ride.status))
    return {status.value: count for status, count in result.all()}


async def list_drivers(db: AsyncSession, status: VerificationStatus | None, q: str | None, limit: int, after_id: int | None) -> list[dict]:
    """Drivers by id, oldest first, with their user and vehicle, rating summary and completed-trips count. One correlated
    count per returned row (through ix_rides_driver_id) rather than a count over every ride."""
    completed_trips = (
        select(func.count()).where(Ride.driver_id == Driver.id, Ride.status == RideStatus.COMPLETED).scalar_subquery()
    )
    query = (
        select(Driver, RatingSummary.rating_count, RatingSummary.rating_total, completed_trips.label("completed_trips"))
        .join(User, User.id == Driver.user_id)
        .outerjoin(Vehicle, Vehicle.driver_id == Driver.id)
        .outerjoin(RatingSummary, RatingSummary.user_id == Driver.user_id)
        .options(*LOAD_DRIVER)
        .order_by(Driver.id)
        .limit(limit)
    )
    if status is not None:
        query = query.where(Driver.verification_status == status)
    if after_id is not None:
        query = query.where(Driver.id > after_id)
    if q is not None:
        # % and _ are wildcards in LIKE: escape them (and the escape character) so they match themselves.
        pattern = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        matches = [
            User.name.ilike(pattern, escape="\\"), User.email.ilike(pattern, escape="\\"),
            Driver.license_number.ilike(pattern, escape="\\"), Vehicle.plate_number.ilike(pattern, escape="\\"),
        ]
        if q.isascii() and q.isdigit() and len(q) <= 9:
            matches.append(Driver.id == int(q))
        query = query.where(or_(*matches))
    result = await db.execute(query.execution_options(populate_existing=True))
    return [
        {
            "driver": driver, "rating_count": rating_count or 0, "rating_total": rating_total or 0, "completed_trips": trips,
        }
        for driver, rating_count, rating_total, trips in result.all()
    ]


async def list_rides(
    db: AsyncSession, status: RideStatus | None, rider_id: int | None, driver_id: int | None, since: datetime | None,
    until: datetime | None, limit: int, before_id: int | None,
) -> list[dict]:
    query = select(
        Ride.id, Ride.created_at, Ride.status, Ride.rider_id, Ride.driver_id, Ride.pickup_address, Ride.dropoff_address,
        Ride.fare_estimate, Ride.final_fare, Ride.surge_percent, Ride.payment_method,
    )
    if status is not None:
        query = query.where(Ride.status == status)
    if rider_id is not None:
        query = query.where(Ride.rider_id == rider_id)
    if driver_id is not None:
        query = query.where(Ride.driver_id == driver_id)
    if since is not None:
        query = query.where(Ride.created_at >= since)
    if until is not None:
        query = query.where(Ride.created_at < until)
    if before_id is not None:
        query = query.where(Ride.id < before_id)
    result = await db.execute(query.order_by(Ride.id.desc()).limit(limit))
    return [dict(row) for row in result.mappings().all()]


async def get_ride_detail(db: AsyncSession, ride_id: int) -> dict | None:
    """The ride (every column but the trip code) with its rider and, if assigned, its driver, user and vehicle."""
    driver_user = aliased(User)
    result = await db.execute(
        select(
            *RIDE_COLUMNS, User.name.label("rider_name"), User.email.label("rider_email"), Driver.user_id.label("driver_user_id"),
            driver_user.name.label("driver_name"), driver_user.email.label("driver_email"), Vehicle.plate_number,
            Vehicle.model.label("vehicle_model"), Vehicle.color.label("vehicle_color"),
        )
        .join(User, User.id == Ride.rider_id)
        .outerjoin(Driver, Driver.id == Ride.driver_id)
        .outerjoin(driver_user, driver_user.id == Driver.user_id)
        .outerjoin(Vehicle, Vehicle.driver_id == Driver.id)
        .where(Ride.id == ride_id)
    )
    row = result.mappings().first()
    return dict(row) if row is not None else None


async def list_ride_offers(db: AsyncSession, ride_id: int) -> list[dict]:
    result = await db.execute(
        select(
            RideOffer.id, RideOffer.driver_id, RideOffer.status, RideOffer.pickup_distance_m, RideOffer.created_at,
            RideOffer.expires_at, RideOffer.responded_at,
        )
        .where(RideOffer.ride_id == ride_id)
        .order_by(RideOffer.id)
    )
    return [dict(row) for row in result.mappings().all()]


async def get_ride_payment(db: AsyncSession, ride_id: int) -> dict | None:
    result = await db.execute(
        select(Payment.id, Payment.amount, Payment.method, Payment.status, Payment.created_at).where(Payment.ride_id == ride_id)
    )
    row = result.mappings().first()
    return dict(row) if row is not None else None


async def get_ride_earning(db: AsyncSession, ride_id: int) -> dict | None:
    result = await db.execute(
        select(
            RideEarning.id, RideEarning.kind, RideEarning.gross_amount, RideEarning.commission_percent, RideEarning.platform_fee,
            RideEarning.driver_earning, RideEarning.created_at,
        )
        .where(RideEarning.ride_id == ride_id)
    )
    row = result.mappings().first()
    return dict(row) if row is not None else None


async def list_ride_ratings(db: AsyncSession, ride_id: int) -> list[dict]:
    """Both ratings of a ride with their comments: only admins read this."""
    result = await db.execute(
        select(Rating.id, Rating.from_user_id, Rating.to_user_id, Rating.score, Rating.comment, Rating.created_at)
        .where(Rating.ride_id == ride_id)
        .order_by(Rating.id)
    )
    return [dict(row) for row in result.mappings().all()]
