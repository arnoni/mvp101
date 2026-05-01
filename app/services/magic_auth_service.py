import time
import secrets
import logging
import uuid
import hashlib
import httpx
from enum import Enum
from typing import Optional, Protocol
from datetime import datetime, timedelta, timezone

from pydantic import BaseModel, EmailStr
from sqlalchemy.ext.asyncio import AsyncEngine
from sqlalchemy import insert, select, text, update, func
from sqlalchemy.dialects.postgresql import insert as pg_insert
from upstash_redis.asyncio import Redis

from app.core.config import settings
from app.models.models import MagicLinkToken, User

logger = logging.getLogger(__name__)

# ==========================================
# 1. MERCHANT OF RECORD (MoR) ABSTRACTION
# ==========================================

class PaymentStatus(str, Enum):
    PAID = "paid"
    PENDING = "pending"
    FAILED = "failed"
    REFUNDED = "refunded"

class PurchaseRecord(BaseModel):
    purchase_id: str
    provider: str
    status: PaymentStatus
    email: EmailStr

class PaymentProvider(Protocol):
    """Protocol allowing easy swapping between MoRs."""
    async def fetch_transaction(self, transaction_id: str) -> Optional[PurchaseRecord]:
        ...

class LemonSqueezyProvider(PaymentProvider):
    async def fetch_transaction(self, transaction_id: str) -> Optional[PurchaseRecord]:
        """Fetches Order by ID from Lemon Squeezy."""
        if not settings.LEMON_SQUEEZY_API_KEY:
            logger.error("LEMON_SQUEEZY_CONFIG_MISSING: API key not configured")
            return None
            
        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(
                    f"https://api.lemonsqueezy.com/v1/orders/{transaction_id}",
                    headers={"Authorization": f"Bearer {settings.LEMON_SQUEEZY_API_KEY}"}
                )
                if response.status_code != 200:
                    logger.warning(f"LEMON_SQUEEZY_FETCH_FAILED: Status {response.status_code} for ID {transaction_id}")
                    return None
                    
                data = response.json().get("data", {}).get("attributes", {})
                
                # Normalize Lemon Squeezy status to internal status
                raw_status = data.get("status")
                status_map = {"paid": PaymentStatus.PAID, "pending": PaymentStatus.PENDING, "failed": PaymentStatus.FAILED}
                
                return PurchaseRecord(
                    purchase_id=transaction_id,
                    provider="lemonsqueezy",
                    status=status_map.get(raw_status, PaymentStatus.PENDING),
                    email=data.get("user_email")
                )
        except Exception as e:
            logger.error(f"LEMON_SQUEEZY_CRITICAL_FAILURE: {e}")
            return None

class PaddleProvider(PaymentProvider):
    async def fetch_transaction(self, transaction_id: str) -> Optional[PurchaseRecord]:
        """Fetches Transaction by ID from Paddle (Billing v2 API)."""
        if not settings.PADDLE_API_KEY:
            logger.error("PADDLE_CONFIG_MISSING: API key not configured")
            return None
            
        try:
            async with httpx.AsyncClient() as client:
                # Use sandbox endpoint if in dev: https://sandbox-api.paddle.com
                base_url = "https://api.paddle.com" if settings.ENV == "production" else "https://sandbox-api.paddle.com"
                response = await client.get(
                    f"{base_url}/transactions/{transaction_id}",
                    headers={"Authorization": f"Bearer {settings.PADDLE_API_KEY}"}
                )
                if response.status_code != 200:
                    logger.warning(f"PADDLE_FETCH_FAILED: Status {response.status_code} for ID {transaction_id}")
                    return None
                    
                data = response.json().get("data", {})
                
                # Normalize Paddle status ('completed' or 'paid' usually mean success)
                raw_status = data.get("status")
                status_map = {"completed": PaymentStatus.PAID, "paid": PaymentStatus.PAID, "ready": PaymentStatus.PENDING}
                
                return PurchaseRecord(
                    purchase_id=transaction_id,
                    provider="paddle",
                    status=status_map.get(raw_status, PaymentStatus.PENDING),
                    email=data.get("customer", {}).get("email_address") or data.get("details", {}).get("customer", {}).get("email_address")
                )
        except Exception as e:
            logger.error(f"PADDLE_CRITICAL_FAILURE: {e}")
            return None

class DodoProvider(PaymentProvider):
    async def fetch_transaction(self, transaction_id: str) -> Optional[PurchaseRecord]:
        """Fetches Payment by ID from Dodo Payments (Simulated for development)."""
        # --- SIMULATION START ---
        # If transaction_id starts with 'sim_', we simulate a successful payment.
        if transaction_id.startswith("sim_"):
            logger.info(f"DODO_SIMULATION: 🚀 Simulating successful Dodo payment for ID: {transaction_id}")
            return PurchaseRecord(
                purchase_id=transaction_id,
                provider="dodo",
                status=PaymentStatus.PAID,
                email="simulated-user@example.com"
            )
        # --- SIMULATION END ---

        if not settings.DODO_API_KEY:
            logger.warning("DODO_CONFIG_MISSING: API key not configured. Falling back to simulation if ID is 'sim_'.")
            return None
            
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                base_url = "https://api.dodopayments.com/v1" if settings.ENV == "production" else "https://test.dodopayments.com/v1"
                response = await client.get(
                    f"{base_url}/payments/{transaction_id}",
                    headers={"Authorization": f"Bearer {settings.DODO_API_KEY}"}
                )
                
                if response.status_code == 404:
                    logger.warning(f"DODO_PAYMENT_NOT_FOUND: {transaction_id}")
                    return None
                    
                response.raise_for_status()
                data = response.json()
                
                # Normalize Dodo status
                raw_status = data.get("status")
                status_map = {"succeeded": PaymentStatus.PAID, "pending": PaymentStatus.PENDING, "failed": PaymentStatus.FAILED}
                
                return PurchaseRecord(
                    purchase_id=transaction_id,
                    provider="dodo",
                    status=status_map.get(raw_status, PaymentStatus.PENDING),
                    email=data.get("customer", {}).get("email")
                )
        except httpx.HTTPError as e:
            logger.error(f"DODO_API_ERROR: API communication error: {e}")
            return None
        except Exception as e:
            logger.error(f"DODO_CRITICAL_FAILURE: Unexpected error: {e}", exc_info=True)
            return None

class PaymentGatewayFactory:
    def get_provider(self, provider_name: str) -> PaymentProvider:
        providers = {
            "lemonsqueezy": LemonSqueezyProvider(),
            "paddle": PaddleProvider(),
            "dodo": DodoProvider()
        }
        if provider_name not in providers:
            raise ValueError(f"Unsupported MoR provider: {provider_name}")
        return providers[provider_name]


# ==========================================
# 2. CORE AUTHENTICATION & SCAM PROTECTION
# ==========================================

class AuthResult(BaseModel):
    success: bool
    user_id: Optional[str] = None
    email: Optional[str] = None
    error: Optional[str] = None
    error_code: Optional[str] = None

class MagicAuthService:
    def __init__(self, db: AsyncEngine, redis: Redis | None, payment_factory: PaymentGatewayFactory):
        self.db = db
        self.redis = redis
        self.payment_factory = payment_factory
        
    async def create_magic_link(
        self,
        email: str,
        purchase_id: Optional[str] = None,
        provider: Optional[str] = None,
        request_ip: str | None = None,
    ) -> str:
        """Generates a one-time token and persists it in Postgres."""
        try:
            del purchase_id, provider
            normalized_email = email.lower()
            expires_at = datetime.now(timezone.utc) + timedelta(minutes=settings.MAGICLINK_EXPIRY_MINUTES)
            token = secrets.token_urlsafe(32)
            token_hash = hashlib.sha256(token.encode()).hexdigest()

            async with self.db.begin() as conn:
                try:
                    upsert_user_stmt = (
                        pg_insert(User)
                        .values(email=normalized_email)
                        .on_conflict_do_update(
                            index_elements=[User.email],
                            set_={"updated_at": func.now()},
                        )
                        .returning(User.id)
                    )
                    user_result = await conn.execute(upsert_user_stmt)
                    user_id = user_result.scalar_one()

                    await conn.execute(
                        insert(MagicLinkToken).values(
                            user_id=user_id,
                            email=normalized_email,
                            token_hash=token_hash,
                            expires_at=expires_at,
                            request_ip=request_ip,
                        )
                    )
                except Exception as e:
                    logger.error(f"MAGIC_AUTH_DB_INSERT_FAILED: {e}")
                    raise
                    
            return token
        except Exception as e:
            logger.exception(f"MAGIC_AUTH_CREATE_TOKEN_FAILED: {e}")
            raise

    async def redeem_token(self, token: str) -> AuthResult:
        """Consumes a Postgres-backed magic link and returns the logged-in user."""
        try:
            token_hash = hashlib.sha256(token.encode()).hexdigest()

            async with self.db.begin() as conn:
                token_result = await conn.execute(
                    select(MagicLinkToken).where(MagicLinkToken.token_hash == token_hash).limit(1)
                )
                token_row = token_result.scalar_one_or_none()
                if token_row is None:
                    return AuthResult(success=False, error="Invalid link", error_code="invalid")
                if token_row.redeemed_at is not None:
                    return AuthResult(success=False, error="Link already used", error_code="consumed")
                if token_row.expires_at <= datetime.now(timezone.utc):
                    return AuthResult(success=False, error="Link expired", error_code="expired")

                await conn.execute(
                    update(MagicLinkToken)
                    .where(MagicLinkToken.id == token_row.id)
                    .values(redeemed_at=func.now())
                )

                user_result = await conn.execute(
                    pg_insert(User)
                    .values(email=token_row.email.lower())
                    .on_conflict_do_update(
                        index_elements=[User.email],
                        set_={"last_login": func.now(), "updated_at": func.now()},
                    )
                    .returning(User.id, User.email)
                )
                user_row = user_result.first()
                user_id = str(uuid.UUID(str(user_row.id)))

            return AuthResult(success=True, user_id=user_id, email=user_row.email)
        except Exception as e:
            logger.exception(f"MAGIC_AUTH_REDEEM_CRITICAL_FAILURE: {e}")
            return AuthResult(success=False, error="Critical authentication failure", error_code="critical_failure")

    async def _verify_payment(self, conn, expected_email: str, purchase_id: str, provider_name: str) -> AuthResult:
        """Strict Verification with Live API Fallback for Webhook Delays"""
        # Check local DB first
        # Assuming you have a 'purchases' table as described in the provided code
        try:
            row = await conn.execute(
                text("SELECT status, email FROM purchases WHERE purchase_id = :pid"),
                {"pid": purchase_id}
            )
            purchase = row.fetchone()
        except Exception:
            # Table might not exist yet, fallback to live check
            purchase = None

        # DELAY PROTECTION: If DB lacks the record OR shows pending (webhook is delayed)
        if not purchase or purchase.status == PaymentStatus.PENDING.value:
            try:
                provider = self.payment_factory.get_provider(provider_name)
                live_record = await provider.fetch_transaction(purchase_id)
                
                if not live_record:
                    return AuthResult(success=False, error="Transaction not found in payment provider", error_code="payment_missing")
                    
                status = live_record.status.value
                buyer_email = live_record.email
                
            except Exception as e:
                logger.error(f"Failed to fetch transaction from {provider_name}: {e}")
                return AuthResult(success=False, error="Could not verify payment with provider", error_code="provider_error")
        else:
            status = purchase.status
            buyer_email = purchase.email

        # SCAM PROTECTION: Ensure payment is fully complete
        if status != PaymentStatus.PAID.value:
            return AuthResult(success=False, error=f"Payment status is {status}. Please wait a moment and try again.", error_code="payment_incomplete")
            
        # SCAM PROTECTION: Stop Cross-Account Transaction Theft
        if not buyer_email or buyer_email.lower() != expected_email.lower():
            logger.warning(f"Payment Theft Attempt! Expected: {expected_email}, Found on Receipt: {buyer_email}")
            return AuthResult(success=False, error="Email mismatch with purchase receipt", error_code="payment_email_mismatch")

        return AuthResult(success=True)
