from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import PaymentMethod, RideStatus, User
from app.repositories import drivers as drivers_repo
from app.repositories import payments as payments_repo
from app.repositories import rides as rides_repo
from app.repositories import wallet as wallet_repo
from app.services import rides as rides_service

RECEIPT_PREFIX = "RCPT-"


async def get_receipt(db: AsyncSession, user: User, ride_id: int) -> dict:
    """The receipt of a ride the rider was charged for. Derived from what was stored at settlement (the breakdown, the
    payment, the ledger entry) and never recomputed, so a later change to the pricing rule or to surge cannot change it."""
    ride = await rides_service.load_ride_for_user(db, user, ride_id)
    # Only the ride's own rider: anyone else gets the same answer as for an unknown ride.
    if ride.rider_id != user.id:
        raise HTTPException(status_code=404, detail="Ride not found")
    payment = await payments_repo.get_payment_by_ride(db, ride.id)
    if ride.status not in (RideStatus.COMPLETED, RideStatus.CANCELLED) or payment is None:
        raise HTTPException(status_code=409, detail="There is no receipt for this ride")

    driver = await drivers_repo.get_by_id(db, ride.driver_id)
    wallet_balance_after = await wallet_repo.get_charge_entry(db, ride.id) if payment.method == PaymentMethod.wallet else None
    if ride.status == RideStatus.COMPLETED:
        ended_at = ride.completed_at
    else:
        ended_at = await rides_repo.get_last_event_time(db, ride.id, RideStatus.CANCELLED)

    breakdown = ride.fare_breakdown
    trip = cancellation = None
    if breakdown["kind"] == "trip":
        trip = {
            "distance_m": breakdown["distance_m"],
            "duration_s": breakdown["duration_s"],
            "distance_source": breakdown["distance_source"],
            "base_fare": breakdown["base_fare"],
            "distance_fare": breakdown["distance_fare"],
            "time_fare": breakdown["time_fare"],
            "minimum_fare_applied": breakdown["minimum_fare_applied"],
            # Breakdowns written before M5.2 have no surge keys: they were not surged.
            "normal_fare": breakdown.get("normal_fare", breakdown["computed_fare"]),
            "surge_percent": breakdown.get("surge_percent", 100),
            "surge_amount": breakdown.get("surge_amount", 0),
            "computed_fare": breakdown["computed_fare"],
            "capped": breakdown["capped"],
            "total": ride.final_fare,
        }
    else:
        cancellation = {"fee": breakdown["fee"], "reason": breakdown["reason"], "cancelled_by": breakdown["cancelled_by"]}

    return {
        "receipt_number": f"{RECEIPT_PREFIX}{ride.id:08d}",
        "issued_at": payment.created_at,
        "ride_id": ride.id,
        "kind": breakdown["kind"],
        "status": ride.status,
        "pickup_address": ride.pickup_address,
        "dropoff_address": ride.dropoff_address,
        "started_at": ride.started_at,
        "ended_at": ended_at,
        "driver_name": driver.user.name,
        "vehicle": {"plate_number": driver.vehicle.plate_number, "model": driver.vehicle.model, "color": driver.vehicle.color},
        "estimated_fare": ride.fare_estimate,
        "trip": trip,
        "cancellation": cancellation,
        "payment": {
            "method": payment.method, "amount": payment.amount, "status": payment.status, "wallet_balance_after": wallet_balance_after,
        },
    }
