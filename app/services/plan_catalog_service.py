from dataclasses import dataclass
from typing import Optional

from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncEngine


@dataclass
class PlanConfig:
    code: str
    amount_usd_cents: int
    currency: str
    dodo_product_id: Optional[str]
    display_price: str


def _is_missing_billing_plans_error(exc: ProgrammingError) -> bool:
    original = getattr(exc, "orig", None)
    sqlstate = getattr(original, "sqlstate", None)
    if sqlstate == "42P01":
        return True
    return "billing_plans" in str(exc)


async def get_plan_by_code(db_engine: AsyncEngine | None, plan_code: str) -> PlanConfig | None:
    if db_engine is None:
        return None

    try:
        async with db_engine.connect() as conn:
            result = await conn.execute(
                text(
                    """
                    SELECT code, amount_usd_cents, currency, dodo_product_id, display_price
                    FROM billing_plans
                    WHERE code = :code AND is_active = true
                    LIMIT 1
                    """
                ),
                {"code": plan_code},
            )
            row = result.mappings().first()
            if not row:
                return None
            return PlanConfig(
                code=row["code"],
                amount_usd_cents=int(row["amount_usd_cents"]),
                currency=row["currency"],
                dodo_product_id=row.get("dodo_product_id"),
                display_price=row["display_price"],
            )
    except ProgrammingError as exc:
        if _is_missing_billing_plans_error(exc):
            return None
        raise


async def get_active_plan_prices(db_engine: AsyncEngine | None) -> dict[str, str]:
    if db_engine is None:
        return {}

    try:
        async with db_engine.connect() as conn:
            result = await conn.execute(
                text(
                    """
                    SELECT code, display_price
                    FROM billing_plans
                    WHERE is_active = true
                    """
                )
            )
            return {row.code: row.display_price for row in result.fetchall()}
    except ProgrammingError as exc:
        if _is_missing_billing_plans_error(exc):
            return {}
        raise
