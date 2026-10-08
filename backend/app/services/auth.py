from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import User, UserRole
from app.repositories import users as users_repo
from app.schemas import LoginRequest, RegisterRequest, TokenResponse, UserResponse
from app.security import create_access_token, hash_password, verify_password

# Verified when the email is unknown, so a missing user takes as long as a wrong password.
DUMMY_PASSWORD_HASH = hash_password("dummy-password")


async def register(db: AsyncSession, data: RegisterRequest) -> User:
    email = data.email.lower()
    if await users_repo.get_by_email(db, email) is not None:
        raise HTTPException(status_code=409, detail="Email already registered")
    if data.phone is not None and await users_repo.get_by_phone(db, data.phone) is not None:
        raise HTTPException(status_code=409, detail="Phone already registered")

    user = await users_repo.create(
        db, UserRole(data.role), data.name, email, data.phone, hash_password(data.password)
    )
    await db.commit()
    return user


async def login(db: AsyncSession, data: LoginRequest) -> TokenResponse:
    user = await users_repo.get_by_email(db, data.email.lower())
    password_hash = user.password_hash if user is not None else DUMMY_PASSWORD_HASH
    password_ok = verify_password(data.password, password_hash)
    if user is None or not password_ok:
        raise HTTPException(status_code=401, detail="Invalid email or password")

    return TokenResponse(
        access_token=create_access_token(user), user=UserResponse.model_validate(user)
    )
