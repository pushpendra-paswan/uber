from datetime import datetime, timedelta, timezone

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.models import User
from app.repositories import users as users_repo

password_hasher = PasswordHasher()
# auto_error=False so we return our own 401 (with WWW-Authenticate) when the header is missing.
bearer_scheme = HTTPBearer(auto_error=False)


def hash_password(password: str) -> str:
    return password_hasher.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return password_hasher.verify(password_hash, password)
    except VerifyMismatchError:
        return False


def create_access_token(user: User) -> str:
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=settings.access_token_expire_minutes)
    claims = {"sub": str(user.id), "role": user.role.value, "exp": expires_at}
    return jwt.encode(claims, settings.jwt_secret, algorithm=settings.jwt_algorithm)


# Exception to the layering rule: auth code with no business rule may call the repository directly.
# Shared by HTTP (get_current_user) and the WebSocket router. Returns None for any problem with the token.
async def user_from_token(db: AsyncSession, token: str) -> User | None:
    try:
        claims = jwt.decode(
            token,
            settings.jwt_secret,
            algorithms=[settings.jwt_algorithm],
            options={"require": ["sub", "exp"]},
        )
        user_id = int(claims["sub"])
    except (jwt.InvalidTokenError, ValueError):
        return None
    return await users_repo.get_by_id(db, user_id)


async def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    db: AsyncSession = Depends(get_db),
) -> User:
    unauthorized = HTTPException(
        status_code=401, detail="Not authenticated", headers={"WWW-Authenticate": "Bearer"}
    )
    if credentials is None:
        raise unauthorized
    user = await user_from_token(db, credentials.credentials)
    if user is None:
        raise unauthorized
    return user


def require_role(*roles: str):
    async def check_role(user: User = Depends(get_current_user)) -> User:
        if user.role.value not in roles:
            raise HTTPException(status_code=403, detail="You do not have permission to do this")
        return user

    return check_role
