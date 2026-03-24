import time
import secrets
import logging
import json
import httpx
from enum import Enum
from typing import Optional, Protocol
from datetime import datetime, timedelta, timezone

from pydantic import BaseModel, EmailStr
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
from sqlalchemy.ext.asyncio import AsyncEngine
from sqlalchemy import text
from upstash_redis.asyncio import Redis

from app.core.config import settings

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
    user_id: Optional[int] = None
    email: Optional[str] = None
    error: Optional[str] = None
    error_code: Optional[str] = None

class MagicAuthService:
    def __init__(self, db: AsyncEngine, redis: Redis, payment_factory: PaymentGatewayFactory):
        self.db = db
        self.redis = redis
        self.payment_factory = payment_factory
        self.serializer = URLSafeTimedSerializer(settings.SECRET_KEY, salt="magic-auth-v3")
        
    async def create_magic_link(self, email: str, purchase_id: Optional[str] = None, provider: Optional[str] = None) -> str:
        """Generates token payload containing payment refs & stores idempotency row."""
        try:
            expires_at = datetime.now(timezone.utc) + timedelta(minutes=15)
            token_id = secrets.token_urlsafe(16)
            
            payload = {
                "email": email.lower(),
                "exp": int(expires_at.timestamp()),
                "jti": token_id,
                "pid": purchase_id,
                "prv": provider
            }
            
            try:
                token = self.serializer.dumps(payload)
            except Exception as e:
                logger.error(f"MAGIC_AUTH_SERIALIZATION_FAILED: {e}")
                raise

            async with self.db.begin() as conn:
                try:
                    await conn.execute(
                        text("""
                            INSERT INTO magic_link_tokens (email, token_hash, expires_at, purpose)
                            VALUES (:email, :token_id, :expires_at, 'login')
                        """),
                        {"email": email.lower(), "token_id": token_id, "expires_at": expires_at}
                    )
                except Exception as e:
                    logger.error(f"MAGIC_AUTH_DB_INSERT_FAILED: {e}")
                    raise
                    
            return token
        except Exception as e:
            logger.exception(f"MAGIC_AUTH_CREATE_TOKEN_FAILED: {e}")
            raise

    async def redeem_token(self, token: str) -> AuthResult:
        """Atomically checks token, validates MoR payment safely, and logs user in."""
        try:
            try:
                payload = self.serializer.loads(token, max_age=900)
                email = payload["email"]
                token_id = payload["jti"]
                purchase_id = payload.get("pid")
                provider_name = payload.get("prv")
            except SignatureExpired:
                return AuthResult(success=False, error="Link expired", error_code="expired")
            except BadSignature:
                return AuthResult(success=False, error="Invalid link signature", error_code="invalid")
            except Exception as e:
                logger.error(f"MAGIC_AUTH_TOKEN_LOAD_FAILED: {e}")
                return AuthResult(success=False, error="Invalid token", error_code="token_invalid")

            async with self.db.begin() as conn:
                # 1. ATOMIC CONSUMPTION: UPDATE ... RETURNING to prevent double-click race conditions
                try:
                    result = await conn.execute(
                        text("""
                            UPDATE magic_link_tokens 
                            SET redeemed_at = NOW() 
                            WHERE token_hash = :token_id AND redeemed_at IS NULL AND expires_at > NOW()
                            RETURNING id
                        """),
                        {"token_id": token_id}
                    )
                    if not result.fetchone():
                        return AuthResult(success=False, error="Link already used or expired", error_code="consumed")
                except Exception as e:
                    logger.error(f"MAGIC_AUTH_DB_REDEEM_FAILED: {e}")
                    return AuthResult(success=False, error="Database error during redemption", error_code="db_error")

                # 2. PAYMENT & SCAM VALIDATION
                if purchase_id and provider_name:
                    try:
                        valid_payment = await self._verify_payment(conn, email, purchase_id, provider_name)
                        if not valid_payment.success:
                            return valid_payment
                    except Exception as e:
                        logger.error(f"MAGIC_AUTH_PAYMENT_VERIFY_FAILED: {e}")
                        return AuthResult(success=False, error="Payment verification failed", error_code="payment_verify_failed")
                    
                # 3. UPSERT USER
                try:
                    user_result = await conn.execute(
                        text("""
                            INSERT INTO users (email, created_at) 
                            VALUES (:email, NOW()) 
                            ON CONFLICT (email) DO UPDATE SET last_login = NOW()
                            RETURNING id
                        """),
                        {"email": email}
                    )
                    user_id = user_result.scalar()
                except Exception as e:
                    logger.error(f"MAGIC_AUTH_DB_USER_UPSERT_FAILED: {e}")
                    return AuthResult(success=False, error="Database error during user login", error_code="user_upsert_failed")

            return AuthResult(success=True, user_id=user_id, email=email)
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