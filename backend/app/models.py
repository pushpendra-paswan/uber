import enum
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, Enum, Float, ForeignKey, Integer, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base

# Enum names equal their values, so the string stored in Postgres is exactly the value.
# Stored as plain strings (native_enum=False), so adding a value needs no Postgres enum-type migration.


class UserRole(enum.Enum):
    rider = "rider"
    driver = "driver"
    admin = "admin"


class VerificationStatus(enum.Enum):
    pending = "pending"
    approved = "approved"
    rejected = "rejected"


class RideStatus(enum.Enum):
    REQUESTED = "REQUESTED"
    DRIVER_ASSIGNED = "DRIVER_ASSIGNED"
    DRIVER_ARRIVED = "DRIVER_ARRIVED"
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"
    NO_DRIVER_FOUND = "NO_DRIVER_FOUND"


class PaymentMethod(enum.Enum):
    cash = "cash"
    wallet = "wallet"
    card = "card"


class PaymentStatus(enum.Enum):
    pending = "pending"
    succeeded = "succeeded"
    failed = "failed"
    refunded = "refunded"


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    role: Mapped[UserRole] = mapped_column(Enum(UserRole, native_enum=False))
    name: Mapped[str] = mapped_column(String(100))
    email: Mapped[str] = mapped_column(String(255), unique=True)
    phone: Mapped[str | None] = mapped_column(String(20), unique=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Driver(Base):
    __tablename__ = "drivers"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), unique=True)
    license_number: Mapped[str] = mapped_column(String(50))
    verification_status: Mapped[VerificationStatus] = mapped_column(
        Enum(VerificationStatus, native_enum=False), default=VerificationStatus.pending, server_default="pending"
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    # lazy="raise": async cannot lazy load, so a forgotten eager load fails loudly.
    user: Mapped["User"] = relationship(lazy="raise")
    vehicle: Mapped["Vehicle | None"] = relationship(lazy="raise")


class Vehicle(Base):
    __tablename__ = "vehicles"

    id: Mapped[int] = mapped_column(primary_key=True)
    driver_id: Mapped[int] = mapped_column(ForeignKey("drivers.id"), unique=True)
    plate_number: Mapped[str] = mapped_column(String(20), unique=True)
    model: Mapped[str] = mapped_column(String(50))
    color: Mapped[str] = mapped_column(String(30))
    vehicle_type: Mapped[str] = mapped_column(String(30), default="economy", server_default="economy")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Ride(Base):
    __tablename__ = "rides"

    id: Mapped[int] = mapped_column(primary_key=True)
    rider_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    driver_id: Mapped[int | None] = mapped_column(ForeignKey("drivers.id"), index=True)
    pickup_lat: Mapped[float] = mapped_column(Float)
    pickup_lng: Mapped[float] = mapped_column(Float)
    pickup_address: Mapped[str] = mapped_column(String(255))
    dropoff_lat: Mapped[float] = mapped_column(Float)
    dropoff_lng: Mapped[float] = mapped_column(Float)
    dropoff_address: Mapped[str] = mapped_column(String(255))
    status: Mapped[RideStatus] = mapped_column(
        Enum(RideStatus, native_enum=False), default=RideStatus.REQUESTED, server_default="REQUESTED", index=True
    )
    distance_m: Mapped[int | None] = mapped_column(Integer)
    duration_s: Mapped[int | None] = mapped_column(Integer)
    fare_estimate: Mapped[int | None] = mapped_column(Integer)  # paise
    final_fare: Mapped[int | None] = mapped_column(Integer)  # paise
    otp: Mapped[str | None] = mapped_column(String(4))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    events: Mapped[list["RideEvent"]] = relationship(order_by="RideEvent.id")


class RideEvent(Base):
    __tablename__ = "ride_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    ride_id: Mapped[int] = mapped_column(ForeignKey("rides.id"), index=True)
    from_status: Mapped[RideStatus | None] = mapped_column(Enum(RideStatus, native_enum=False))
    to_status: Mapped[RideStatus] = mapped_column(Enum(RideStatus, native_enum=False))
    actor_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Payment(Base):
    __tablename__ = "payments"

    id: Mapped[int] = mapped_column(primary_key=True)
    ride_id: Mapped[int] = mapped_column(ForeignKey("rides.id"), index=True)
    amount: Mapped[int] = mapped_column(Integer)  # paise
    method: Mapped[PaymentMethod] = mapped_column(Enum(PaymentMethod, native_enum=False))
    status: Mapped[PaymentStatus] = mapped_column(
        Enum(PaymentStatus, native_enum=False), default=PaymentStatus.pending, server_default="pending"
    )
    idempotency_key: Mapped[str] = mapped_column(String(100), unique=True)
    gateway_ref: Mapped[str | None] = mapped_column(String(100), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Rating(Base):
    __tablename__ = "ratings"
    __table_args__ = (
        CheckConstraint("score >= 1 AND score <= 5", name="score_range"),
        UniqueConstraint("ride_id", "from_user_id", name="uq_ratings_ride_id_from_user_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    ride_id: Mapped[int] = mapped_column(ForeignKey("rides.id"))
    from_user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    to_user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    score: Mapped[int] = mapped_column(Integer)
    comment: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class PricingRule(Base):
    __tablename__ = "pricing_rules"

    id: Mapped[int] = mapped_column(primary_key=True)
    vehicle_type: Mapped[str] = mapped_column(String(30), unique=True)
    base_fare: Mapped[int] = mapped_column(Integer)  # paise
    per_km: Mapped[int] = mapped_column(Integer)  # paise
    per_min: Mapped[int] = mapped_column(Integer)  # paise
    min_fare: Mapped[int] = mapped_column(Integer)  # paise
    surge_cap: Mapped[float] = mapped_column(Float, default=2.0, server_default="2.0")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
