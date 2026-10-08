"""Create an admin user. Admins can never be created through the API.

Usage: docker compose exec backend python create_admin.py --email ... --name ... --password ...
"""
import argparse
import asyncio

from app.database import async_session
from app.models import UserRole
from app.repositories import users as users_repo
from app.security import hash_password


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--email", required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument("--password", required=True)
    args = parser.parse_args()

    if len(args.password) < 8:
        raise SystemExit("Password must be at least 8 characters")

    email = args.email.lower()
    async with async_session() as db:
        if await users_repo.get_by_email(db, email) is not None:
            print(f"User with email {email} already exists. Nothing changed.")
            return
        user = await users_repo.create(
            db, UserRole.admin, args.name, email, None, hash_password(args.password)
        )
        await db.commit()
        print(f"Created admin id={user.id} email={email}")


asyncio.run(main())
