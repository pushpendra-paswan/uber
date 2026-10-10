from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import User
from app.repositories import pricing as pricing_repo
from app.schemas import RULE_LIMITS, PricingRulePatch

CHANGES_DEFAULT_LIMIT = 10
CHANGES_MAX_LIMIT = 100


async def get_rules(db: AsyncSession) -> dict:
    return {"rules": await pricing_repo.list_rules(db), "active_rides": await pricing_repo.count_active_rides(db), "limits": RULE_LIMITS}


async def update_rule(db: AsyncSession, admin: User, vehicle_type: str, data: PricingRulePatch) -> dict:
    """Optimistic edit: the request carries the version the admin saw. Lock the rule row, THEN read it and check the version
    (a separate statement, so it sees what the previous holder committed), write, bump the version, add the audit row, commit.
    A stale version is a 409 and changes nothing, so replaying an applied request fails and can never apply twice: no
    idempotency key is needed. The rule row is a leaf lock: nothing else is locked while it is held, and readers
    (pricing_repo.get_rule) take no lock, so a settlement running during an edit sees the old or the new rule, never a torn one."""
    rule_id = await pricing_repo.lock_rule(db, vehicle_type)
    if rule_id is None:
        raise HTTPException(status_code=404, detail="Pricing rule not found")
    rule = await pricing_repo.get_rule(db, vehicle_type)
    if rule.version != data.version:
        raise HTTPException(status_code=409, detail="This rule was changed by someone else. Reload and try again.")

    changes = []
    for field in RULE_LIMITS:  # the editable fields, in a fixed order
        if field not in data.model_fields_set:
            continue
        new = getattr(data, field)
        if field == "surge_cap":
            new = round(new * 100) / 100  # at most two decimals were checked; this removes float noise (2 and 2.00 equal 2.0)
        old = getattr(rule, field)
        if new != old:
            changes.append({"field": field, "old": old, "new": new})

    if not changes:
        await db.rollback()  # nothing to write: release the lock now
        updated = await pricing_repo.list_rules(db)
    else:
        await pricing_repo.update_rule(db, rule_id, {change["field"]: change["new"] for change in changes}, rule.version + 1, admin.id)
        await pricing_repo.insert_change(db, rule_id, admin.id, rule.version, rule.version + 1, changes)
        await db.commit()
        updated = await pricing_repo.list_rules(db)

    return {
        "rule": next(row for row in updated if row["id"] == rule_id),
        "active_rides": await pricing_repo.count_active_rides(db),
        "changes": changes,
    }


async def list_changes(db: AsyncSession, vehicle_type: str, limit: int) -> list[dict]:
    if not 1 <= limit <= CHANGES_MAX_LIMIT:
        raise HTTPException(status_code=422, detail=f"limit must be between 1 and {CHANGES_MAX_LIMIT}")
    rule = await pricing_repo.get_rule(db, vehicle_type)
    if rule is None:
        raise HTTPException(status_code=404, detail="Pricing rule not found")
    return await pricing_repo.list_changes(db, rule.id, limit)
