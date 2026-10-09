from datetime import datetime
from typing import Annotated, Literal

from fastapi import Header
from pydantic import BaseModel, ConfigDict, EmailStr, Field, StrictInt, model_validator

from app.models import PaymentMethod, RideStatus, TopupStatus, UserRole, VerificationStatus, WalletEntryKind

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


# What a rider (or the assigned driver, or an admin) may know about the driver of a ride:
# no email, phone, license number, or verification status.
class RideDriverResponse(BaseModel):
    driver_id: int
    name: str
    vehicle: VehicleResponse | None
    location: DriverLocation | None


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
