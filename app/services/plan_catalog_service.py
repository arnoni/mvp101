from dataclasses import dataclass
from typing import Optional

from sqlalchemy import select
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncEngine

from app.models.models import BillingPlan, SimulatedBillingPlan


@dataclass
class PlanConfig:
    code: str
    amount_usd_cents: int
    currency: str
    dodo_product_id: Optional[str]
    display_price: str


@dataclass
class SimulatedPlanConfig:
    code: str
    duration_hours: int


def _is_missing_billing_plans_error(exc: ProgrammingError) -> bool:
    original = getattr(exc, "orig", None)
    sqlstate = getattr(original, "sqlstate", None)
    if sqlstate == "42P01":
        return True
    return "billing_plans" in str(exc)


def _is_missing_simulated_billing_plans_error(exc: ProgrammingError) -> bool:
    original = getattr(exc, "orig", None)
    sqlstate = getattr(original, "sqlstate", None)
    if sqlstate == "42P01":
        return True
    return "simulated_billing_plans" in str(exc)


async def get_plan_by_code(db_engine: AsyncEngine | None, plan_code: str) -> PlanConfig | None:
    if db_engine is None:
        return None

    try:
        async with db_engine.connect() as conn:
            result = await conn.execute(
                select(BillingPlan).where(BillingPlan.code == plan_code, BillingPlan.is_active.is_(True)).limit(1)
            )
            plan = result.scalar_one_or_none()
            if plan is None:
                return None
            return PlanConfig(
                code=plan.code,
                amount_usd_cents=int(plan.amount_usd_cents),
                currency=plan.currency,
                dodo_product_id=plan.dodo_product_id,
                display_price=plan.display_price,
            )
    except ProgrammingError as exc:
        if _is_missing_billing_plans_error(exc):
            return None
        raise


async def get_simulated_plan_by_code(db_engine: AsyncEngine | None, plan_code: str) -> SimulatedPlanConfig | None:
    if db_engine is None:
        return None

    try:
        async with db_engine.connect() as conn:
            result = await conn.execute(
                select(SimulatedBillingPlan).where(
                    SimulatedBillingPlan.code == plan_code,
                    SimulatedBillingPlan.is_active.is_(True),
                ).limit(1)
            )
            plan = result.scalar_one_or_none()
            if plan is None:
                return None
            return SimulatedPlanConfig(code=plan.code, duration_hours=int(plan.duration_hours))
    except ProgrammingError as exc:
        if _is_missing_simulated_billing_plans_error(exc):
            return None
        raise


async def get_active_plan_prices(db_engine: AsyncEngine | None) -> dict[str, str]:
    if db_engine is None:
        return {}

    try:
        async with db_engine.connect() as conn:
            result = await conn.execute(select(BillingPlan.code, BillingPlan.display_price).where(BillingPlan.is_active.is_(True)))
            return {row.code: row.display_price for row in result.fetchall()}
    except ProgrammingError as exc:
        if _is_missing_billing_plans_error(exc):
            return {}
        raise
