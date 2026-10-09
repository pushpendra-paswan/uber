from fastapi import APIRouter, Depends, Header, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import User, WalletTopup
from app.schemas import IdempotencyKey, TopupCreate, TopupResponse, WalletEntryResponse, WalletResponse
from app.security import require_role
from app.services import payments as payments_service
from app.services import wallet as wallet_service

# No prefix: the wallet routes belong to riders, the webhook belongs to Stripe.
router = APIRouter(tags=["payments"])


@router.get("/wallet", response_model=WalletResponse)
async def get_wallet(user: User = Depends(require_role("rider")), db: AsyncSession = Depends(get_db)) -> dict:
    return await wallet_service.get_wallet(db, user)


@router.get("/wallet/entries", response_model=list[WalletEntryResponse])
async def list_entries(
    limit: int = wallet_service.ENTRIES_DEFAULT_LIMIT, before_id: int | None = None,
    user: User = Depends(require_role("rider")), db: AsyncSession = Depends(get_db),
) -> list[dict]:
    return await wallet_service.list_entries(db, user, limit, before_id)


@router.post("/wallet/topups", response_model=TopupResponse, status_code=201)
async def create_topup(
    data: TopupCreate, response: Response, key: IdempotencyKey,
    user: User = Depends(require_role("rider")), db: AsyncSession = Depends(get_db),
) -> WalletTopup:
    topup, created = await payments_service.create_topup(db, user, key, data)
    if not created:
        response.status_code = 200
    return topup


@router.get("/wallet/topups", response_model=list[TopupResponse])
async def list_topups(
    limit: int = 10, user: User = Depends(require_role("rider")), db: AsyncSession = Depends(get_db)
) -> list[WalletTopup]:
    return await payments_service.list_topups(db, user, limit)


@router.post("/wallet/topups/{topup_id}/sync", response_model=TopupResponse)
async def sync_topup(
    topup_id: int, user: User = Depends(require_role("rider")), db: AsyncSession = Depends(get_db)
) -> WalletTopup:
    return await payments_service.sync_topup(db, user, topup_id)


# No login: the signature is the authentication. It needs the raw bytes, exactly as Stripe sent them.
@router.post("/webhooks/stripe")
async def stripe_webhook(
    request: Request, stripe_signature: str | None = Header(default=None), db: AsyncSession = Depends(get_db)
) -> dict:
    return await payments_service.process_webhook(db, await request.body(), stripe_signature)
