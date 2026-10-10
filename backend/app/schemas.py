from datetime import datetime
from typing import Annotated, Literal

from fastapi import Header
from pydantic import AwareDatetime, BaseModel, ConfigDict, EmailStr, Field, StrictInt, field_validator, model_validator

from app.models import OfferStatus, PaymentMethod, PaymentStatus, RideStatus, TopupStatus, UserRole, VerificationStatus, WalletEntryKind

# The editable pricing values and their allowed range (money in paise). The one source of the bounds: the Field constraints of
# PricingRulePatch use it, and GET /admin/pricing-rules returns it so the page never hard-codes a range. Only the safety floors
# and the surge_cap range are also database checks, so the upper bounds can be raised without a migration.
RULE_LIMITS = {
    "base_fare": {"min": 0, "max": 100000},
    "per_km": {"min": 0, "max": 50000},
    "per_min": {"min": 0, "max": 20000},
    "min_fare": {"min": 100, "max": 500000},
    "cancellation_fee": {"min": 0, "max": 100000},
    "free_cancel_seconds": {"min": 0, "max": 3600},
    "commission_percent": {"min": 0, "max": 100},
    "surge_cap": {"min": 1.0, "max": 2.0},
}

# The Idempotency-Key header of a request that must not happen twice: 8 to 64 characters, required.
IdempotencyKey = Annotated[str, Header(alias="Idempotency-Key", min_length=8, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")]


class RegisterRequest(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    email: EmailStr
    phone: str | None = Field(default=None, max_length=20)
    password: str = Field(min_length=8, max_length=128)
    role: Literal["rider", "driver"]


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class UserResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    role: UserRole
    name: str
    email: str
    phone: str | None
    created_at: datetime


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: UserResponse


class DriverProfileCreate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    license_number: str = Field(min_length=5, max_length=30)


class VehicleCreate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    # The pattern requires at least one letter or digit, so "---" cannot become an empty plate.
    plate_number: str = Field(min_length=1, max_length=20, pattern=r"[A-Za-z0-9]")
    model: str = Field(min_length=1, max_length=50)
    color: str = Field(min_length=1, max_length=30)


class VehicleResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    plate_number: str
    model: str
    color: str
    vehicle_type: str


class DriverResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    license_number: str
    verification_status: VerificationStatus
    created_at: datetime
    user: UserResponse
    vehicle: VehicleResponse | None


class LocationUpdate(BaseModel):
    lat: float = Field(ge=-90, le=90)
    lng: float = Field(ge=-180, le=180)


class PresenceResponse(BaseModel):
    online: bool
    lat: float | None = None
    lng: float | None = None
    updated_at: int | None = None  # epoch seconds of the last update


class DriverLocation(BaseModel):
    lat: float
    lng: float
    updated_at: int  # epoch seconds of the last update


# What other people see of someone's ratings: how many, and the average only once there are enough to hide each person.
class RatingPublic(BaseModel):
    count: int
    average: float | None


# What a rider (or the assigned driver, or an admin) may know about the driver of a ride:
# no email, phone, license number, or verification status.
class RideDriverResponse(BaseModel):
    driver_id: int
    name: str
    vehicle: VehicleResponse | None
    location: DriverLocation | None
    rating: RatingPublic


class EstimateRequest(BaseModel):
    pickup_lat: float = Field(ge=-90, le=90)
    pickup_lng: float = Field(ge=-180, le=180)
    dropoff_lat: float = Field(ge=-90, le=90)
    dropoff_lng: float = Field(ge=-180, le=180)

    @model_validator(mode="after")
    def pickup_and_dropoff_differ(self) -> "EstimateRequest":
        if (self.pickup_lat, self.pickup_lng) == (self.dropoff_lat, self.dropoff_lng):
            raise ValueError("Pickup and drop-off must be different places")
        return self


class EstimateResponse(BaseModel):
    distance_m: int
    duration_s: int
    fare_estimate: int  # paise, the total with surge
    base_fare: int  # base_fare, distance_fare and time_fare are the normal parts, before surge
    distance_fare: int
    time_fare: int
    minimum_fare_applied: bool
    normal_fare: int  # after the minimum fare, before surge
    surge_percent: int  # 100 is no surge, 150 is 1.5x
    surge_amount: int  # fare_estimate - normal_fare
    max_fare: int  # the most this trip can cost: the fare cap, paise
    path: list[list[float]]  # [lat, lng] pairs


class RideCreate(EstimateRequest):
    model_config = ConfigDict(str_strip_whitespace=True)

    pickup_address: str = Field(min_length=1, max_length=255)
    dropoff_address: str = Field(min_length=1, max_length=255)
    # The multiplier the rider saw in the estimate. Absent means "accept the current one" (scripts and simulators).
    accepted_surge_percent: int | None = Field(default=None, ge=100, le=200)
    # Chosen once and never changed. Absent means cash, so scripts and simulators keep working.
    payment_method: Literal["cash", "wallet"] = "cash"


class StartTripRequest(BaseModel):
    # [0-9] and not \d: \d also matches digits of other scripts, which secrets.compare_digest cannot compare.
    otp: str = Field(pattern=r"^[0-9]{4}$")


# The only place the trip code leaves the backend. RideResponse must never get an otp field.
class OtpResponse(BaseModel):
    otp: str


class RideResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    rider_id: int
    driver_id: int | None
    pickup_lat: float
    pickup_lng: float
    pickup_address: str
    dropoff_lat: float
    dropoff_lng: float
    dropoff_address: str
    status: RideStatus
    distance_m: int | None
    duration_s: int | None
    fare_estimate: int | None
    surge_percent: int
    final_fare: int | None
    actual_distance_m: int | None
    actual_duration_s: int | None
    fare_breakdown: dict | None
    payment_method: PaymentMethod
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None


# What a driver may know about an offered ride. The pickup and drop-off names are the same as in RideResponse
# on purpose, so the driver page can draw an offer and a ride alike. No rider name, email, or phone.
class OfferResponse(BaseModel):
    id: int
    ride_id: int
    pickup_address: str
    pickup_lat: float
    pickup_lng: float
    dropoff_address: str
    dropoff_lat: float
    dropoff_lng: float
    trip_distance_m: int | None
    trip_duration_s: int | None
    fare_estimate: int | None  # paise
    pickup_distance_m: int
    expires_in: float  # seconds left, never negative


class SurgeZoneResponse(BaseModel):
    zone: str
    demand: int
    supply: int
    pressure_percent: int
    surge_percent: int  # the step value, before the cap of the pricing rule


class SurgeSnapshotResponse(BaseModel):
    computed_at: int  # epoch seconds
    age_seconds: int
    zones: list[SurgeZoneResponse]


class CancellationFeeResponse(BaseModel):
    fee: int  # paise
    reason: str


class RideEventResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    from_status: RideStatus | None
    to_status: RideStatus
    actor_user_id: int | None
    created_at: datetime


class MapConfigResponse(BaseModel):
    city_name: str
    center_lat: float
    center_lng: float
    zoom: int
    south: float
    west: float
    north: float
    east: float


class PlaceResponse(BaseModel):
    display_name: str
    lat: float
    lng: float


class WalletResponse(BaseModel):
    balance: int  # paise
    reserved: int  # the most the rider's current wallet ride can cost, or 0
    available: int  # balance - reserved


class WalletEntryResponse(BaseModel):
    id: int
    amount: int  # paise, signed
    kind: WalletEntryKind
    balance_after: int
    ride_id: int | None
    topup_id: int | None
    note: str | None
    created_at: datetime


class TopupCreate(BaseModel):
    amount: StrictInt  # paise; the limits are checked by the service


class TopupResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    amount: int
    status: TopupStatus
    checkout_url: str | None
    created_at: datetime
    completed_at: datetime | None

    @model_validator(mode="after")
    def hide_finished_checkout_url(self) -> "TopupResponse":
        # The page is only worth opening while the top-up is waiting for the payment.
        if self.status != TopupStatus.PENDING:
            self.checkout_url = None
        return self


class AdjustRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    amount: StrictInt  # paise, signed, not 0; the limit is checked by the service
    note: str = Field(min_length=1, max_length=200)


class EarningsBucket(BaseModel):
    rides: int
    gross: int  # paise
    platform_fee: int
    driver_earning: int


class EarningsSettlement(BaseModel):
    owed_to_driver: int  # paise: the platform collected these fares (wallet rides) and owes the driver its part
    owed_by_driver: int  # the driver collected these fares (cash rides) and owes the platform its fee
    net: int  # owed_to_driver - owed_by_driver; positive means the platform owes the driver


class EarningsSummaryResponse(BaseModel):
    since: datetime | None  # the window as it was asked for, inclusive
    until: datetime | None  # exclusive
    trips: int
    cancellation_fees: int
    total: EarningsBucket
    cash: EarningsBucket
    wallet: EarningsBucket
    settlement: EarningsSettlement


# What a driver may know about a settled ride: the addresses of their own ride, and nothing about the rider.
class EarningEntryResponse(BaseModel):
    id: int
    ride_id: int
    kind: Literal["trip", "cancellation"]
    payment_method: PaymentMethod
    gross_amount: int
    commission_percent: int
    platform_fee: int
    driver_earning: int
    pickup_address: str
    dropoff_address: str
    distance_m: int | None
    duration_s: int | None
    created_at: datetime


# What a rider may know about the driver of a paid ride: the name and the vehicle, no id and no contact data.
class ReceiptVehicle(BaseModel):
    plate_number: str
    model: str
    color: str


class ReceiptTrip(BaseModel):
    distance_m: int
    duration_s: int
    distance_source: str  # "tracked" or "estimate"
    base_fare: int
    distance_fare: int
    time_fare: int
    minimum_fare_applied: bool
    normal_fare: int
    surge_percent: int
    surge_amount: int
    computed_fare: int
    capped: bool
    total: int


class ReceiptCancellation(BaseModel):
    fee: int
    reason: str
    cancelled_by: str


class ReceiptPayment(BaseModel):
    method: PaymentMethod
    amount: int
    status: PaymentStatus
    wallet_balance_after: int | None


# No otp, no commission, no earning, no driver id. Exactly one of trip and cancellation is set.
class ReceiptResponse(BaseModel):
    receipt_number: str
    issued_at: datetime
    ride_id: int
    kind: Literal["trip", "cancellation"]
    status: RideStatus
    pickup_address: str
    dropoff_address: str
    started_at: datetime | None
    ended_at: datetime | None
    driver_name: str
    vehicle: ReceiptVehicle
    estimated_fare: int | None
    trip: ReceiptTrip | None
    cancellation: ReceiptCancellation | None
    payment: ReceiptPayment


class RatingCreate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    score: StrictInt = Field(ge=1, le=5)  # strict: "5", 4.5 and true are refused
    comment: str | None = Field(default=None, max_length=300)

    @field_validator("comment")
    @classmethod
    def empty_comment_is_none(cls, comment: str | None) -> str | None:
        return comment or None


# What the rater may know about their own rating: no user ids.
class RatingResponse(BaseModel):
    id: int
    ride_id: int
    score: int
    comment: str | None
    created_at: datetime


class RatingStatusResponse(BaseModel):
    can_rate: bool
    reason: Literal["not_completed", "already_rated", "window_closed"] | None
    expires_at: datetime | None  # the end of the rating window, for a COMPLETED ride
    mine: RatingResponse | None  # the caller's own rating; the other person's is never read


# Your own ratings: the real average, even when there are only one or two.
class RatingSummaryResponse(BaseModel):
    count: int
    average: float | None


# The only place a score is shown together with its comment and both people, for admins.
class AdminRatingResponse(BaseModel):
    id: int
    ride_id: int
    from_user_id: int
    to_user_id: int
    score: int
    comment: str | None
    created_at: datetime


# ---- Admin dashboard (M6.2). None of these has an otp field, and none has a password hash. ----

DriverState = Literal["offline", "free", "offered", "on_ride"]


# Every value is optional except the version. A value that is present must be valid, so an explicit null is a 422 (the default
# None is never validated). Money is a strict integer in paise: strings, floats and booleans are refused.
class PricingRulePatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: StrictInt  # the version the admin saw; a stale one is a 409
    base_fare: StrictInt = Field(default=None, ge=RULE_LIMITS["base_fare"]["min"], le=RULE_LIMITS["base_fare"]["max"])
    per_km: StrictInt = Field(default=None, ge=RULE_LIMITS["per_km"]["min"], le=RULE_LIMITS["per_km"]["max"])
    per_min: StrictInt = Field(default=None, ge=RULE_LIMITS["per_min"]["min"], le=RULE_LIMITS["per_min"]["max"])
    min_fare: StrictInt = Field(default=None, ge=RULE_LIMITS["min_fare"]["min"], le=RULE_LIMITS["min_fare"]["max"])
    cancellation_fee: StrictInt = Field(
        default=None, ge=RULE_LIMITS["cancellation_fee"]["min"], le=RULE_LIMITS["cancellation_fee"]["max"]
    )
    free_cancel_seconds: StrictInt = Field(
        default=None, ge=RULE_LIMITS["free_cancel_seconds"]["min"], le=RULE_LIMITS["free_cancel_seconds"]["max"]
    )
    commission_percent: StrictInt = Field(
        default=None, ge=RULE_LIMITS["commission_percent"]["min"], le=RULE_LIMITS["commission_percent"]["max"]
    )
    # An int or a float, never a string or a boolean (strict), with at most two decimals.
    surge_cap: float = Field(default=None, strict=True, ge=RULE_LIMITS["surge_cap"]["min"], le=RULE_LIMITS["surge_cap"]["max"])

    @field_validator("surge_cap")
    @classmethod
    def at_most_two_decimals(cls, surge_cap: float) -> float:
        if abs(round(surge_cap * 100) / 100 - surge_cap) > 1e-9:
            raise ValueError("surge_cap can have at most two decimals")
        return surge_cap

    @model_validator(mode="after")
    def has_a_change(self) -> "PricingRulePatch":
        if not self.model_fields_set - {"version"}:
            raise ValueError("Send at least one value to change")
        return self


class PricingRuleResponse(BaseModel):
    id: int
    vehicle_type: str
    base_fare: int
    per_km: int
    per_min: int
    min_fare: int
    cancellation_fee: int
    free_cancel_seconds: int
    commission_percent: int
    surge_cap: float
    version: int
    updated_at: datetime
    updated_by: int | None
    updated_by_name: str | None


class PricingRulesResponse(BaseModel):
    rules: list[PricingRuleResponse]
    active_rides: int  # rides in REQUESTED, DRIVER_ASSIGNED, DRIVER_ARRIVED or IN_PROGRESS right now
    limits: dict[str, dict[str, int | float]]


class PricingRuleChangeItem(BaseModel):
    field: str
    old: int | float
    new: int | float


class PricingRuleUpdateResponse(BaseModel):
    rule: PricingRuleResponse
    active_rides: int
    changes: list[PricingRuleChangeItem]  # empty when nothing differed: then nothing was written


class PricingRuleChangeResponse(BaseModel):
    id: int
    actor_name: str
    version_before: int
    version_after: int
    changes: list[PricingRuleChangeItem]
    created_at: datetime


class LiveDriver(BaseModel):
    id: int
    name: str
    plate_number: str | None
    lat: float
    lng: float
    state: DriverState
    active_ride_id: int | None


class LiveRide(BaseModel):
    id: int
    status: RideStatus
    pickup_lat: float
    pickup_lng: float
    pickup_address: str
    dropoff_lat: float
    dropoff_lng: float
    dropoff_address: str
    driver_id: int | None
    rider_id: int
    rider_name: str
    created_at: datetime
    fare_estimate: int | None


class LiveDriverCounts(BaseModel):
    online: int
    free: int
    offered: int
    on_ride: int


class LiveCounts(BaseModel):
    drivers: LiveDriverCounts
    rides: dict[str, int]


class LiveResponse(BaseModel):
    generated_at: int  # epoch seconds
    drivers: list[LiveDriver]
    rides: list[LiveRide]
    drivers_total: int  # exact, even when the list is cut
    rides_total: int
    truncated: bool  # true when either list was cut at its cap
    counts: LiveCounts


# Everything in DriverResponse plus what the admin needs to find and judge a driver. rating_average is the REAL average,
# even with one rating (an admin is not a rider).
class AdminDriverResponse(DriverResponse):
    user_id: int
    online: bool
    state: DriverState
    active_ride_id: int | None
    rating_count: int
    rating_average: float | None
    completed_trips: int


class AdminRideRow(BaseModel):
    id: int
    created_at: datetime
    status: RideStatus
    rider_id: int
    driver_id: int | None
    pickup_address: str
    dropoff_address: str
    fare_estimate: int | None
    final_fare: int | None
    surge_percent: int
    payment_method: PaymentMethod


class AdminRideRider(BaseModel):
    id: int
    name: str
    email: str


class AdminRideDriver(BaseModel):
    id: int
    user_id: int
    name: str
    email: str
    plate_number: str | None
    model: str | None
    color: str | None


class AdminRideEvent(BaseModel):
    id: int
    from_status: RideStatus | None
    to_status: RideStatus
    actor_user_id: int | None
    actor: Literal["system", "rider", "driver", "other"]
    created_at: datetime


class AdminRideOffer(BaseModel):
    id: int
    driver_id: int
    status: OfferStatus
    pickup_distance_m: int
    created_at: datetime
    expires_at: datetime
    responded_at: datetime | None


class AdminRidePayment(BaseModel):
    id: int
    amount: int
    method: PaymentMethod
    status: PaymentStatus
    created_at: datetime


class AdminRideEarning(BaseModel):
    id: int
    kind: str
    gross_amount: int
    commission_percent: int
    platform_fee: int
    driver_earning: int
    created_at: datetime


class AdminRideRating(BaseModel):
    id: int
    from_user_id: int
    to_user_id: int
    from_role: Literal["rider", "driver"]
    score: int
    comment: str | None
    created_at: datetime


class AdminRideDetail(AdminRideRow):
    pickup_lat: float
    pickup_lng: float
    dropoff_lat: float
    dropoff_lng: float
    distance_m: int | None
    duration_s: int | None
    actual_distance_m: int | None
    actual_duration_s: int | None
    fare_breakdown: dict | None
    started_at: datetime | None
    completed_at: datetime | None
    rider: AdminRideRider
    driver: AdminRideDriver | None
    events: list[AdminRideEvent]
    offers: list[AdminRideOffer]
    payment: AdminRidePayment | None
    earning: AdminRideEarning | None
    ratings: list[AdminRideRating]


class StatsRides(BaseModel):
    requested: int
    completed: int
    cancelled: int
    no_driver_found: int
    active: int
    completion_rate: float | None  # percent, one decimal, of the finished rides (completed + cancelled + no_driver_found)
    cancellation_rate: float | None
    no_driver_rate: float | None


class StatsTrips(BaseModel):
    avg_fare: int | None  # paise
    avg_distance_m: int | None
    avg_duration_s: int | None
    assigned_rides: int
    mean_time_to_assign_s: float | None
    median_time_to_assign_s: float | None


class StatsOffers(BaseModel):
    pending: int
    accepted: int
    rejected: int
    expired: int
    cancelled: int
    acceptance_rate: float | None  # percent: accepted / (accepted + rejected + expired)


class StatsSurge(BaseModel):
    rides_surged: int
    max_surge_percent: int


class StatsUsers(BaseModel):
    new_riders: int
    new_drivers: int


class StatsNow(BaseModel):
    online_drivers: int | None  # null when Redis is down
    active_rides: dict[str, int]
    pending_offers: int


class StatsBucket(BaseModel):
    start: int  # epoch seconds
    rides: int
    completed: int
    gross: int
    platform_fee: int


class StatsResponse(BaseModel):
    since: AwareDatetime
    until: AwareDatetime
    bucket: Literal["hour", "day"]
    utc_offset_minutes: int
    rides: StatsRides
    trips: StatsTrips
    offers: StatsOffers
    surge: StatsSurge
    money: EarningsSummaryResponse
    users: StatsUsers
    now: StatsNow
    series: list[StatsBucket]


# ---- Trip history and saved places (M6.3). No row has an otp field, and the two kinds of row share no person data. ----

# What a rider may know about a past trip: the driver's NAME only (no id, contact data, vehicle, commission or earning), and
# only the rider's OWN rating. distance_m and duration_s are the tracked values when the trip was settled, else the estimate.
class RiderTripRow(BaseModel):
    id: int
    status: RideStatus
    created_at: datetime  # the request time
    ended_at: datetime | None  # the time of the ride's last event
    pickup_address: str
    dropoff_address: str
    distance_m: int | None
    duration_s: int | None
    final_fare: int | None  # paise, as stored: null for NO_DRIVER_FOUND and old rides, 0 for a free cancellation
    payment_method: PaymentMethod
    driver_name: str | None
    cancelled_by: Literal["rider", "driver"] | None
    has_receipt: bool  # a payment row exists
    my_rating: int | None
    can_rate: bool


# What a driver may know about a past trip: nothing about the rider, and their own fare split.
class DriverTripRow(BaseModel):
    id: int
    status: RideStatus
    created_at: datetime
    ended_at: datetime | None
    pickup_address: str
    dropoff_address: str
    distance_m: int | None
    duration_s: int | None
    fare: int | None  # the stored final_fare, paise
    payment_method: PaymentMethod
    platform_fee: int | None  # null when the ride has no earning row
    driver_earning: int | None
    cancelled_by: Literal["rider", "driver"] | None
    my_rating: int | None
    can_rate: bool


# No control characters in a label or an address (they are shown as text, but a newline would break every list).
# strict: a number, never a string or a boolean. The range is also checked here, so 91 is a 422 before any lock.
NO_CONTROL_CHARACTERS = r"^[^\x00-\x1f\x7f]+$"


class SavedPlaceCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    label: str = Field(min_length=1, max_length=30, pattern=NO_CONTROL_CHARACTERS)
    address: str = Field(min_length=1, max_length=200, pattern=NO_CONTROL_CHARACTERS)
    lat: float = Field(strict=True, ge=-90, le=90)
    lng: float = Field(strict=True, ge=-180, le=180)


class SavedPlaceRename(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    label: str = Field(min_length=1, max_length=30, pattern=NO_CONTROL_CHARACTERS)


class SavedPlaceResponse(BaseModel):
    id: int
    label: str
    address: str
    lat: float
    lng: float
    created_at: datetime
