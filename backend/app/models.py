import enum
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, Enum, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import JSONB
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


ACTIVE_RIDE_STATUSES = (
    RideStatus.REQUESTED,
    RideStatus.DRIVER_ASSIGNED,
    RideStatus.DRIVER_ARRIVED,
    RideStatus.IN_PROGRESS,
)


class OfferStatus(enum.Enum):
    PENDING = "PENDING"
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    CANCELLED = "CANCELLED"


class PaymentMethod(enum.Enum):
    cash = "cash"
    wallet = "wallet"
    card = "card"


class PaymentStatus(enum.Enum):
    pending = "pending"
    succeeded = "succeeded"
    failed = "failed"
    refunded = "refunded"


class WalletEntryKind(enum.Enum):
    TOPUP = "TOPUP"
    RIDE_CHARGE = "RIDE_CHARGE"
    ADJUSTMENT = "ADJUSTMENT"


class TopupStatus(enum.Enum):
    PENDING = "PENDING"
    SUCCEEDED = "SUCCEEDED"
    EXPIRED = "EXPIRED"


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
    __table_args__ = (
        # Safety net behind the row locks: even if a lock is missed, the database refuses a second active ride.
        # The conditions match the stored enum values (upper-case names, plain strings).
        Index(
            "uq_rides_one_active_per_driver", "driver_id", unique=True,
            postgresql_where=text("status IN ('DRIVER_ASSIGNED', 'DRIVER_ARRIVED', 'IN_PROGRESS')"),
        ),
        Index(
            "uq_rides_one_active_per_rider", "rider_id", unique=True,
            postgresql_where=text("status IN ('REQUESTED', 'DRIVER_ASSIGNED', 'DRIVER_ARRIVED', 'IN_PROGRESS')"),
        ),
        CheckConstraint("surge_percent BETWEEN 100 AND 200", name="surge_percent_range"),
        # The surge demand query: rides created in the last few minutes.
        Index("ix_rides_created_at", "created_at"),
    )

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
    fare_estimate: Mapped[int | None] = mapped_column(Integer)  # paise, with the surge multiplier of surge_percent
    # The geohash cell of the pickup (NULL for rides from before M5.2) and the multiplier locked at request time, in
    # integer percent: 100 is no surge, 150 is 1.5x. Settlement reuses this multiplier, never the current one.
    pickup_zone: Mapped[str | None] = mapped_column(String(12))
    surge_percent: Mapped[int] = mapped_column(Integer, default=100, server_default="100")
    # What the rider owes, set once when the ride is settled: the trip fare (COMPLETED) or the cancellation fee (CANCELLED).
    final_fare: Mapped[int | None] = mapped_column(Integer)  # paise
    actual_distance_m: Mapped[int | None] = mapped_column(Integer)  # the distance that was billed
    actual_duration_s: Mapped[int | None] = mapped_column(Integer)
    # Amounts, not rates: a later change to the pricing rule does not touch old rides.
    fare_breakdown: Mapped[dict | None] = mapped_column(JSONB)
    # Chosen when the ride is requested and never changed. Cash is collected by the driver; wallet is charged at settlement.
    payment_method: Mapped[PaymentMethod] = mapped_column(
        Enum(PaymentMethod, native_enum=False), default=PaymentMethod.cash, server_default="cash"
    )
    otp: Mapped[str | None] = mapped_column(String(4))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # lazy="raise": events are read through the repository, never through the ride.
    events: Mapped[list["RideEvent"]] = relationship(order_by="RideEvent.id", lazy="raise")


class RideEvent(Base):
    __tablename__ = "ride_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    ride_id: Mapped[int] = mapped_column(ForeignKey("rides.id"), index=True)
    from_status: Mapped[RideStatus | None] = mapped_column(Enum(RideStatus, native_enum=False))
    to_status: Mapped[RideStatus] = mapped_column(Enum(RideStatus, native_enum=False))
    actor_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class RideOffer(Base):
    __tablename__ = "ride_offers"
    __table_args__ = (
        # A driver is never offered the same ride twice.
        UniqueConstraint("ride_id", "driver_id", name="uq_ride_offers_ride_id_driver_id"),
        # A ride has at most one open offer at a time. The condition matches the stored enum value.
        Index("uq_ride_offers_one_pending_per_ride", "ride_id", unique=True, postgresql_where=text("status = 'PENDING'")),
        # A driver has at most one open offer at a time. It counts an offer past its deadline that the sweeper has not
        # handled yet, because now() cannot appear in an index condition; availability counts it too.
        Index("uq_ride_offers_one_pending_per_driver", "driver_id", unique=True, postgresql_where=text("status = 'PENDING'")),
        # The sweeper's query: PENDING offers, oldest deadline first.
        Index("ix_ride_offers_status_expires_at", "status", "expires_at"),
        # The admin stats windows: offers created in a period.
        Index("ix_ride_offers_created_at", "created_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    ride_id: Mapped[int] = mapped_column(ForeignKey("rides.id"))
    driver_id: Mapped[int] = mapped_column(ForeignKey("drivers.id"), index=True)
    status: Mapped[OfferStatus] = mapped_column(
        Enum(OfferStatus, native_enum=False), default=OfferStatus.PENDING, server_default="PENDING"
    )
    pickup_distance_m: Mapped[int] = mapped_column(Integer)  # straight line, driver to pickup, when the offer was made
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    responded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))  # only ACCEPTED and REJECTED


class Payment(Base):
    __tablename__ = "payments"
    __table_args__ = (CheckConstraint("amount > 0", name="amount_positive"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    # Unique: a ride is charged once, at settlement. A fee of 0 creates no payment.
    ride_id: Mapped[int] = mapped_column(ForeignKey("rides.id"), unique=True)
    amount: Mapped[int] = mapped_column(Integer)  # paise
    method: Mapped[PaymentMethod] = mapped_column(Enum(PaymentMethod, native_enum=False))
    status: Mapped[PaymentStatus] = mapped_column(
        Enum(PaymentStatus, native_enum=False), default=PaymentStatus.pending, server_default="pending"
    )
    idempotency_key: Mapped[str] = mapped_column(String(100), unique=True)
    gateway_ref: Mapped[str | None] = mapped_column(String(100), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class RideEarning(Base):
    __tablename__ = "ride_earnings"
    __table_args__ = (
        CheckConstraint("kind IN ('trip', 'cancellation')", name="kind_known"),
        CheckConstraint("gross_amount > 0", name="gross_positive"),
        CheckConstraint("commission_percent BETWEEN 0 AND 100", name="commission_percent_range"),
        CheckConstraint("platform_fee >= 0", name="platform_fee_not_negative"),
        CheckConstraint("driver_earning >= 0", name="driver_earning_not_negative"),
        # Conservation: the split never creates or loses a paisa.
        CheckConstraint("platform_fee + driver_earning = gross_amount", name="split_adds_up"),
        Index("ix_ride_earnings_driver_id_id", "driver_id", "id"),
        Index("ix_ride_earnings_created_at", "created_at"),
    )

    # Written by payments.charge_ride in the same transaction as the payment, and never updated or deleted: the amounts and
    # the percent are snapshots, so a later change to the pricing rule leaves old rows alone.
    id: Mapped[int] = mapped_column(primary_key=True)
    ride_id: Mapped[int] = mapped_column(ForeignKey("rides.id"), unique=True)
    payment_id: Mapped[int] = mapped_column(ForeignKey("payments.id"), unique=True)
    driver_id: Mapped[int] = mapped_column(ForeignKey("drivers.id"))
    kind: Mapped[str] = mapped_column(String(20))  # "trip" or "cancellation", as in rides.fare_breakdown
    gross_amount: Mapped[int] = mapped_column(Integer)  # paise, equal to the payment
    commission_percent: Mapped[int] = mapped_column(Integer)  # the rate at settlement time
    platform_fee: Mapped[int] = mapped_column(Integer)  # paise
    driver_earning: Mapped[int] = mapped_column(Integer)  # paise
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Wallet(Base):
    __tablename__ = "wallets"

    # One row per user, created lazily by the first ledger write. balance always equals the sum of the user's entries.
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), primary_key=True)
    balance: Mapped[int] = mapped_column(Integer, default=0, server_default="0")  # paise
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class WalletTopup(Base):
    __tablename__ = "wallet_topups"
    __table_args__ = (
        UniqueConstraint("user_id", "idempotency_key", name="uq_wallet_topups_user_id_idempotency_key"),
        CheckConstraint("amount > 0", name="amount_positive"),
        Index("ix_wallet_topups_user_id_id", "user_id", "id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    amount: Mapped[int] = mapped_column(Integer)  # paise
    status: Mapped[TopupStatus] = mapped_column(
        Enum(TopupStatus, native_enum=False), default=TopupStatus.PENDING, server_default="PENDING"
    )
    idempotency_key: Mapped[str] = mapped_column(String(64))
    stripe_session_id: Mapped[str | None] = mapped_column(String(255), unique=True)
    stripe_payment_intent_id: Mapped[str | None] = mapped_column(String(255), unique=True)
    checkout_url: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class WalletEntry(Base):
    __tablename__ = "wallet_entries"
    __table_args__ = (
        UniqueConstraint("user_id", "idempotency_key", name="uq_wallet_entries_user_id_idempotency_key"),
        # Safety nets behind the locks: a ride is charged once and a top-up is credited once, whatever the code does.
        # The conditions match the stored enum values (upper-case names, plain strings).
        Index("uq_wallet_entries_one_charge_per_ride", "ride_id", unique=True, postgresql_where=text("kind = 'RIDE_CHARGE'")),
        Index("uq_wallet_entries_one_credit_per_topup", "topup_id", unique=True, postgresql_where=text("kind = 'TOPUP'")),
        CheckConstraint("amount <> 0", name="amount_nonzero"),
        CheckConstraint("(kind = 'RIDE_CHARGE') = (ride_id IS NOT NULL)", name="ride_id_matches_kind"),
        CheckConstraint("(kind = 'TOPUP') = (topup_id IS NOT NULL)", name="topup_id_matches_kind"),
        CheckConstraint("(kind <> 'RIDE_CHARGE' OR amount < 0) AND (kind <> 'TOPUP' OR amount > 0)", name="amount_sign_matches_kind"),
        Index("ix_wallet_entries_user_id_id", "user_id", "id"),
    )

    # Append-only: rows are inserted by services/wallet.post_entry and never updated or deleted.
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    amount: Mapped[int] = mapped_column(Integer)  # paise, signed: positive credits, negative debits
    kind: Mapped[WalletEntryKind] = mapped_column(Enum(WalletEntryKind, native_enum=False))
    balance_after: Mapped[int] = mapped_column(Integer)
    ride_id: Mapped[int | None] = mapped_column(ForeignKey("rides.id"))
    topup_id: Mapped[int | None] = mapped_column(ForeignKey("wallet_topups.id"))
    idempotency_key: Mapped[str | None] = mapped_column(String(64))
    note: Mapped[str | None] = mapped_column(String(200))
    actor_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class StripeEvent(Base):
    __tablename__ = "stripe_events"

    # The Stripe event id: inserting it is the first step of processing a webhook, which is how duplicates are told apart.
    id: Mapped[str] = mapped_column(String(255), primary_key=True)
    event_type: Mapped[str] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Rating(Base):
    __tablename__ = "ratings"
    __table_args__ = (
        CheckConstraint("score >= 1 AND score <= 5", name="score_range"),
        UniqueConstraint("ride_id", "from_user_id", name="uq_ratings_ride_id_from_user_id"),
        CheckConstraint("from_user_id <> to_user_id", name="no_self_rating"),
        CheckConstraint("comment IS NULL OR char_length(comment) <= 300", name="comment_length"),
        # The admin list: one person's ratings, newest first.
        Index("ix_ratings_to_user_id_id", "to_user_id", "id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    ride_id: Mapped[int] = mapped_column(ForeignKey("rides.id"))
    from_user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    to_user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    score: Mapped[int] = mapped_column(Integer)
    comment: Mapped[str | None] = mapped_column(String(500))  # the check constraint keeps it to 300; private to the author and admins
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class RatingSummary(Base):
    __tablename__ = "rating_summaries"
    __table_args__ = (
        CheckConstraint("rating_count >= 0", name="count_not_negative"),
        # Every score is 1 to 5.
        CheckConstraint("rating_total BETWEEN rating_count AND rating_count * 5", name="total_within_range"),
    )

    # One row per rated user, changed only by repositories/ratings.add_to_summary (one atomic upsert in the rating's own
    # transaction). The average is derived: see utils/ratings.average.
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), primary_key=True)
    rating_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    rating_total: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class SavedPlace(Base):
    __tablename__ = "saved_places"
    __table_args__ = (
        CheckConstraint("char_length(label) BETWEEN 1 AND 30", name="label_length"),
        CheckConstraint("char_length(address) BETWEEN 1 AND 200", name="address_length"),
        CheckConstraint("lat BETWEEN -90 AND 90", name="lat_range"),
        CheckConstraint("lng BETWEEN -180 AND 180", name="lng_range"),
        # One label per rider, whatever the letter case. The safety net behind the owner-row lock in services/saved_places.
        Index("uq_saved_places_user_id_lower_label", "user_id", text("lower(label)"), unique=True),
        Index("ix_saved_places_user_id_id", "user_id", "id"),
    )

    # A text snapshot of a place the rider chose: no geocoding, no link to rides (deleting a place never touches a trip).
    # The cap of 10 per rider cannot be a constraint (the database cannot count); the owner-row lock enforces it.
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    label: Mapped[str] = mapped_column(String(30))
    address: Mapped[str] = mapped_column(String(200))
    lat: Mapped[float] = mapped_column(Float)
    lng: Mapped[float] = mapped_column(Float)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class PricingRule(Base):
    __tablename__ = "pricing_rules"
    __table_args__ = (
        CheckConstraint("commission_percent BETWEEN 0 AND 100", name="commission_percent_range"),
        # Safety floors and the cap range only. The upper bounds of the other values are API-level (RULE_LIMITS in schemas.py),
        # so they can be raised later without a migration. 100 paise keeps every trip chargeable; 2.0 matches rides.surge_percent.
        CheckConstraint("base_fare >= 0", name="base_fare_nonneg"),
        CheckConstraint("per_km >= 0", name="per_km_nonneg"),
        CheckConstraint("per_min >= 0", name="per_min_nonneg"),
        CheckConstraint("cancellation_fee >= 0", name="cancellation_fee_nonneg"),
        CheckConstraint("free_cancel_seconds >= 0", name="free_cancel_seconds_nonneg"),
        CheckConstraint("min_fare >= 100", name="min_fare_floor"),
        CheckConstraint("surge_cap BETWEEN 1.0 AND 2.0", name="surge_cap_range"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    vehicle_type: Mapped[str] = mapped_column(String(30), unique=True)
    base_fare: Mapped[int] = mapped_column(Integer)  # paise
    per_km: Mapped[int] = mapped_column(Integer)  # paise
    per_min: Mapped[int] = mapped_column(Integer)  # paise
    min_fare: Mapped[int] = mapped_column(Integer)  # paise
    surge_cap: Mapped[float] = mapped_column(Float, default=2.0, server_default="2.0")
    cancellation_fee: Mapped[int] = mapped_column(Integer, server_default="3000")  # paise
    free_cancel_seconds: Mapped[int] = mapped_column(Integer, server_default="120")
    commission_percent: Mapped[int] = mapped_column(Integer, server_default="20")  # the platform's share of every payment
    # Optimistic version: an edit carries the version the admin saw, and every applied edit adds 1 (see PricingRuleChange).
    version: Mapped[int] = mapped_column(Integer, server_default="1")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class PricingRuleChange(Base):
    __tablename__ = "pricing_rule_changes"
    __table_args__ = (
        UniqueConstraint("rule_id", "version_after", name="uq_pricing_rule_changes_rule_id_version_after"),
        CheckConstraint("version_after = version_before + 1", name="version_step"),
        Index("ix_pricing_rule_changes_rule_id_id", "rule_id", "id"),
    )

    # Append-only audit trail, inserted by the same transaction that changes the rule and never updated or deleted.
    id: Mapped[int] = mapped_column(primary_key=True)
    rule_id: Mapped[int] = mapped_column(ForeignKey("pricing_rules.id"))
    actor_user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    version_before: Mapped[int] = mapped_column(Integer)
    version_after: Mapped[int] = mapped_column(Integer)
    changes: Mapped[list] = mapped_column(JSONB)  # [{"field": ..., "old": ..., "new": ...}]
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
