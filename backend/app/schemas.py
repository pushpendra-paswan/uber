from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field

from app.models import UserRole, VerificationStatus


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
