from datetime import datetime

from sqlalchemy import and_, exists, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.models import Driver, Payment, Rating, Ride, RideEarning, RideEvent, RideStatus, User

# Read-only queries of the trip history (M6.3): they take no locks, flush nothing and commit nothing. Each page is ONE query
# (the last event time is a scalar subquery, the payment an EXISTS, the rating, the driver name and the earning outer joins),
# never one query per row. Rows are newest first by ride id (request order) and paged with before_id.

# The time of the ride's LAST event: for a finished ride, the terminal transition. NULL when there are no events.
LAST_EVENT_TIME = (
    select(RideEvent.created_at).where(RideEvent.ride_id == Ride.id).order_by(RideEvent.id.desc()).limit(1).scalar_subquery()
)

# The columns both kinds of row share. Distance and time are the tracked values when the trip was settled, else the
# estimate. cancelled_by is stored in the breakdown of a cancelled ride only.
COMMON_COLUMNS = (
    Ride.id,
    Ride.status,
    Ride.created_at,
    LAST_EVENT_TIME.label("ended_at"),
    Ride.pickup_address,
    Ride.dropoff_address,
    func.coalesce(Ride.actual_distance_m, Ride.distance_m).label("distance_m"),
    func.coalesce(Ride.actual_duration_s, Ride.duration_s).label("duration_s"),
    Ride.payment_method,
    Ride.fare_breakdown["cancelled_by"].astext.label("cancelled_by"),
    Ride.completed_at,  # for can_rate; not part of the response
)


def filter_rides(query, statuses: tuple[RideStatus, ...], since: datetime | None, until: datetime | None, before_id: int | None):
    """since is inclusive and until exclusive, both compared with the request time (rides.created_at)."""
    query = query.where(Ride.status.in_(statuses))
    if since is not None:
        query = query.where(Ride.created_at >= since)
    if until is not None:
        query = query.where(Ride.created_at < until)
    if before_id is not None:
        query = query.where(Ride.id < before_id)
    return query


async def list_rider(
    db: AsyncSession, rider_id: int, statuses: tuple[RideStatus, ...], since: datetime | None, until: datetime | None,
    limit: int, before_id: int | None,
) -> list[dict]:
    """The rider's finished rides. The rider is the caller, so their user id is also the rater of my_rating. The driver is
    reached only for the name."""
    driver_user = aliased(User)
    query = (
        select(
            *COMMON_COLUMNS,
            Ride.final_fare,
            driver_user.name.label("driver_name"),
            exists().where(Payment.ride_id == Ride.id).label("has_receipt"),
            Rating.score.label("my_rating"),
        )
        .select_from(Ride)
        .outerjoin(Driver, Driver.id == Ride.driver_id)
        .outerjoin(driver_user, driver_user.id == Driver.user_id)
        .outerjoin(Rating, and_(Rating.ride_id == Ride.id, Rating.from_user_id == rider_id))
        .where(Ride.rider_id == rider_id)
    )
    result = await db.execute(filter_rides(query, statuses, since, until, before_id).order_by(Ride.id.desc()).limit(limit))
    return [dict(row) for row in result.mappings().all()]


async def list_driver(
    db: AsyncSession, driver_id: int, driver_user_id: int, statuses: tuple[RideStatus, ...], since: datetime | None,
    until: datetime | None, limit: int, before_id: int | None,
) -> list[dict]:
    """The driver's finished rides. Nothing about the rider is selected, and the rider's table is not even joined."""
    query = (
        select(
            *COMMON_COLUMNS,
            Ride.final_fare.label("fare"),
            RideEarning.platform_fee,
            RideEarning.driver_earning,
            Rating.score.label("my_rating"),
        )
        .select_from(Ride)
        .outerjoin(RideEarning, RideEarning.ride_id == Ride.id)
        .outerjoin(Rating, and_(Rating.ride_id == Ride.id, Rating.from_user_id == driver_user_id))
        .where(Ride.driver_id == driver_id)
    )
    result = await db.execute(filter_rides(query, statuses, since, until, before_id).order_by(Ride.id.desc()).limit(limit))
    return [dict(row) for row in result.mappings().all()]
