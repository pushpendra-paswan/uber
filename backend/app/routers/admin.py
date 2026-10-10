from typing import Literal

from fastapi import APIRouter, Depends, Response
from pydantic import AwareDatetime
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import Driver, RideStatus, User, VerificationStatus
from app.schemas import (
    AdjustRequest, AdminDriverResponse, AdminRatingResponse, AdminRideDetail, AdminRideRow, DriverResponse, EarningsSummaryResponse,
    IdempotencyKey, LiveResponse, PricingRuleChangeResponse, PricingRulePatch, PricingRulesResponse, PricingRuleUpdateResponse,
    StatsResponse, SurgeSnapshotResponse, WalletEntryResponse,
)
from app.security import require_role
from app.services import admin as admin_service
from app.services import drivers as drivers_service
from app.services import earnings as earnings_service
from app.services import pricing as pricing_service
from app.services import pricing_rules as pricing_rules_service
from app.services import ratings as ratings_service
from app.services import stats as stats_service
from app.services import wallet as wallet_service

router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(require_role("admin"))])


# Oldest first by id; after_id pages. q matches name, email, license, plate (case-insensitive, % and _ literal) and a numeric id.
@router.get("/drivers", response_model=list[AdminDriverResponse])
async def list_drivers(
    status: VerificationStatus | None = None, q: str | None = None, limit: int = admin_service.DRIVERS_DEFAULT_LIMIT,
    after_id: int | None = None, db: AsyncSession = Depends(get_db),
) -> list[dict]:
    return await admin_service.list_drivers(db, status, q, limit, after_id)


@router.post("/drivers/{driver_id}/approve", response_model=DriverResponse)
async def approve_driver(driver_id: int, db: AsyncSession = Depends(get_db)) -> Driver:
    return await drivers_service.set_verification(db, driver_id, VerificationStatus.approved)


@router.post("/drivers/{driver_id}/reject", response_model=DriverResponse)
async def reject_driver(driver_id: int, db: AsyncSession = Depends(get_db)) -> Driver:
    return await drivers_service.set_verification(db, driver_id, VerificationStatus.rejected)


@router.get("/surge", response_model=SurgeSnapshotResponse)
async def get_surge(refresh: bool = False, db: AsyncSession = Depends(get_db)) -> dict:
    return await pricing_service.get_surge_zones(db, refresh)


# since is inclusive and until is exclusive; both need a timezone (a naive time is a 422).
@router.get("/revenue", response_model=EarningsSummaryResponse)
async def get_revenue(
    since: AwareDatetime | None = None, until: AwareDatetime | None = None, db: AsyncSession = Depends(get_db)
) -> dict:
    return await earnings_service.get_revenue(db, since, until)


@router.get("/drivers/{driver_id}/earnings", response_model=EarningsSummaryResponse)
async def get_driver_earnings(
    driver_id: int, since: AwareDatetime | None = None, until: AwareDatetime | None = None, db: AsyncSession = Depends(get_db)
) -> dict:
    return await earnings_service.get_admin_driver_summary(db, driver_id, since, until)


@router.post("/wallets/{user_id}/adjust", response_model=WalletEntryResponse, status_code=201)
async def adjust_wallet(
    user_id: int, data: AdjustRequest, response: Response, key: IdempotencyKey,
    admin: User = Depends(require_role("admin")), db: AsyncSession = Depends(get_db),
) -> dict:
    entry, created = await wallet_service.adjust(db, admin, user_id, key, data)
    if not created:
        response.status_code = 200
    return entry


# The only way to read a rating together with its comment: individual ratings are never shown to the person who was rated.
@router.get("/ratings", response_model=list[AdminRatingResponse])
async def list_ratings(
    user_id: int | None = None, max_score: int | None = None, limit: int = ratings_service.ADMIN_LIST_DEFAULT_LIMIT,
    before_id: int | None = None, db: AsyncSession = Depends(get_db),
) -> list[dict]:
    return await ratings_service.list_for_admin(db, user_id, max_score, limit, before_id)


# The live map: online drivers (presence key) and active rides. Plain reads, not one atomic snapshot.
@router.get("/live", response_model=LiveResponse)
async def get_live(db: AsyncSession = Depends(get_db)) -> dict:
    return await admin_service.get_live(db)


# Newest first by id; before_id pages. since is inclusive and until exclusive, both with a timezone.
@router.get("/rides", response_model=list[AdminRideRow])
async def list_rides(
    status: RideStatus | None = None, rider_id: int | None = None, driver_id: int | None = None,
    since: AwareDatetime | None = None, until: AwareDatetime | None = None, limit: int = admin_service.RIDES_DEFAULT_LIMIT,
    before_id: int | None = None, db: AsyncSession = Depends(get_db),
) -> list[dict]:
    return await admin_service.list_rides(db, status, rider_id, driver_id, since, until, limit, before_id)


@router.get("/rides/{ride_id}", response_model=AdminRideDetail)
async def get_ride(ride_id: int, db: AsyncSession = Depends(get_db)) -> dict:
    return await admin_service.get_ride(db, ride_id)


# Ride counts are by request time, money by settlement time. utc_offset_minutes is -new Date().getTimezoneOffset().
@router.get("/stats", response_model=StatsResponse)
async def get_stats(
    since: AwareDatetime, until: AwareDatetime, bucket: Literal["hour", "day"] = "day", utc_offset_minutes: int = 0,
    db: AsyncSession = Depends(get_db),
) -> dict:
    return await stats_service.get_stats(db, since, until, bucket, utc_offset_minutes)


@router.get("/pricing-rules", response_model=PricingRulesResponse)
async def get_pricing_rules(db: AsyncSession = Depends(get_db)) -> dict:
    return await pricing_rules_service.get_rules(db)


@router.patch("/pricing-rules/{vehicle_type}", response_model=PricingRuleUpdateResponse)
async def update_pricing_rule(
    vehicle_type: str, data: PricingRulePatch, admin: User = Depends(require_role("admin")), db: AsyncSession = Depends(get_db)
) -> dict:
    return await pricing_rules_service.update_rule(db, admin, vehicle_type, data)


@router.get("/pricing-rules/{vehicle_type}/changes", response_model=list[PricingRuleChangeResponse])
async def list_pricing_rule_changes(
    vehicle_type: str, limit: int = pricing_rules_service.CHANGES_DEFAULT_LIMIT, db: AsyncSession = Depends(get_db)
) -> list[dict]:
    return await pricing_rules_service.list_changes(db, vehicle_type, limit)
