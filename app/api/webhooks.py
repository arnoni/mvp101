
import secrets 
import hashlib 
import json 
import logging
from fastapi import APIRouter, Request, HTTPException, Depends
from sqlalchemy.ext.asyncio import AsyncEngine
from sqlalchemy import text
from upstash_redis.asyncio import Redis

from app.core.config import settings
from email_service import EmailService

router = APIRouter()
logger = logging.getLogger(__name__)

# --- HELPERS ---

def verify_paddle_signature(raw_body: bytes, signature: str) -> bool:
    """
    Placeholder for Paddle signature verification.
    In production, use Paddle's SDK or hmac.compare_digest.
    """
    if settings.ENV == "development":
        return True
    # TODO: Implement real HMAC verification
    return False

def verify_dodo_signature(raw_body: bytes, signature: str) -> bool:
    """
    Placeholder for DodoPay signature verification.
    """
    if settings.ENV == "development":
        return True
    return False

def parse_paddle_event(raw_body: bytes):
    """Parses Paddle webhook JSON."""
    try:
        data = json.loads(raw_body)
        # Map Paddle's structure to a common internal format
        # This is a simplified mapping based on Paddle API v2
        return type('Event', (), {
            'id': data.get('event_id'),
            'email': data.get('data', {}).get('customer', {}).get('email'),
            'plan': data.get('data', {}).get('custom_data', {}).get('plan', '1_day'),
            'status': data.get('event_type')
        })
    except (json.JSONDecodeError, AttributeError, KeyError) as e:
        logger.error(f"WEBHOOK_PADDLE_PARSE_FAILED: {e}")
        return None

def parse_dodo_event(raw_body: bytes):
    """Parses DodoPay webhook JSON."""
    try:
        data = json.loads(raw_body)
        return type('Event', (), {
            'id': data.get('payment_id'),
            'email': data.get('customer', {}).get('email'),
            'plan': data.get('metadata', {}).get('plan', '1_day'),
            'status': data.get('status')
        })
    except (json.JSONDecodeError, AttributeError, KeyError) as e:
        logger.error(f"WEBHOOK_DODO_PARSE_FAILED: {e}")
        return None

# --- DEPENDENCIES ---

async def get_services(request: Request):
    return {
        "db": request.app.state.db_engine,
        "redis": request.app.state.redis,
        "email": EmailService()
    }

# --- WEBHOOKS ---

@router.post("/paddle")
async def paddle_webhook(request: Request, services: dict = Depends(get_services)):
    """Paddle Webhook with Idempotency and Magic Link delivery."""
    db = services["db"]
    redis = services["redis"]
    email_service = services["email"]

    try:
        # 1. Capture raw bytes BEFORE parsing
        raw_body = await request.body()
        signature = request.headers.get("Paddle-Signature")
        
        if not verify_paddle_signature(raw_body, signature):
            logger.warning("WEBHOOK_PADDLE_INVALID_SIGNATURE: Invalid Paddle signature detected")
            raise HTTPException(status_code=401, detail="Invalid signature")

        event = parse_paddle_event(raw_body)
        if not event or not event.id or not event.email:
            logger.error(f"WEBHOOK_PADDLE_INCOMPLETE_DATA: {raw_body[:200]}")
            raise HTTPException(status_code=400, detail="Incomplete event data")
        
        # 2. TRANSIENT LOCK: Prevent concurrent webhook processing
        lock_key = f"lock:webhook:{event.id}"
        try:
            acquired = await redis.set(lock_key, "1", nx=True, ex=30)
        except Exception as e:
            logger.error(f"WEBHOOK_PADDLE_REDIS_LOCK_FAILED: {e}")
            raise HTTPException(status_code=500, detail="Redis lock failure")

        if not acquired:
            logger.warning(f"WEBHOOK_PADDLE_CONCURRENT_ATTEMPT: {event.id}")
            raise HTTPException(status_code=409, detail="Concurrent processing")

        try:
            # 3. GROUND TRUTH IDEMPOTENCY: Check the database
            async with db.begin() as conn:
                # Check if event already processed
                try:
                    is_processed = await conn.execute(
                        text("SELECT 1 FROM processed_events WHERE event_id = :eid"),
                        {"eid": event.id}
                    )
                    if is_processed.fetchone():
                        logger.info(f"WEBHOOK_PADDLE_ALREADY_PROCESSED: {event.id}")
                        return {"status": "ok"}
                except Exception as e:
                    logger.error(f"WEBHOOK_PADDLE_DB_CHECK_FAILED: {e}")
                    raise HTTPException(status_code=500, detail="Database check failure")

                # Record purchase
                try:
                    purchase_res = await conn.execute(
                        text("""
                            INSERT INTO purchases (email, plan, provider_event_id, provider, status)
                            VALUES (:email, :plan, :eid, 'paddle', 'paid')
                            RETURNING id
                        """),
                        {"email": event.email, "plan": event.plan, "eid": event.id}
                    )
                    purchase_id = purchase_res.scalar()
                except Exception as e:
                    logger.error(f"WEBHOOK_PADDLE_DB_INSERT_FAILED: {e}")
                    raise HTTPException(status_code=500, detail="Database insert failure")
                
                # Permanently record this webhook event ID
                try:
                    await conn.execute(
                        text("INSERT INTO processed_events (event_id) VALUES (:eid)"),
                        {"eid": event.id}
                    )
                except Exception as e:
                    logger.error(f"WEBHOOK_PADDLE_DB_IDEMPOTENCY_FAILED: {e}")
                    # We continue because the purchase was recorded, but this is a concern
                
            # 4. GENERATE MAGIC LINK & SEND EMAIL (Outside the DB transaction)
            raw_token = secrets.token_urlsafe(32)
            token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
            
            redis_payload = json.dumps({
                "email": event.email,
                "plan": event.plan,
                "purchase_id": str(purchase_id)
            })
            # Store in Redis for 30 minutes
            try:
                await redis.set(f"magic:{token_hash}", redis_payload, ex=1800)
            except Exception as e:
                logger.error(f"WEBHOOK_PADDLE_REDIS_STORE_FAILED: {e}")
                raise HTTPException(status_code=500, detail="Redis storage failure")
            
            # Construct the URL
            app_origin = settings.APP_ORIGIN or "http://localhost:8000"
            magic_url = f"{app_origin}/api/auth/magic?token={raw_token}"
            
            try:
                await email_service.send_magic_link(
                    email=event.email,
                    magic_link=magic_url,
                    expire_minutes=30
                )
            except Exception as e:
                logger.error(f"WEBHOOK_PADDLE_EMAIL_SEND_FAILED: {e}")
                # We don't raise here to avoid Paddle retrying a successful payment, 
                # but the user didn't get their link. Manual intervention might be needed.
            
        finally:
            # 5. RELEASE TRANSIENT LOCK
            try:
                await redis.delete(lock_key)
            except Exception as e:
                logger.error(f"WEBHOOK_PADDLE_REDIS_UNLOCK_FAILED: {e}")

    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"WEBHOOK_PADDLE_CRITICAL_FAILURE: {e}")
        raise HTTPException(status_code=500, detail="Critical webhook failure")

    return {"status": "ok"}

@router.post("/dodo")
async def dodo_webhook(request: Request, services: dict = Depends(get_services)):
    """DodoPay Webhook with Idempotency and Magic Link delivery."""
    db = services["db"]
    redis = services["redis"]
    email_service = services["email"]

    try:
        raw_body = await request.body()
        signature = request.headers.get("Dodo-Signature") # Example header
        
        if not verify_dodo_signature(raw_body, signature):
            logger.warning("WEBHOOK_DODO_INVALID_SIGNATURE: Invalid Dodo signature detected")
            raise HTTPException(status_code=401, detail="Invalid signature")

        event = parse_dodo_event(raw_body)
        if not event or not event.id or not event.email:
            logger.error(f"WEBHOOK_DODO_INCOMPLETE_DATA: {raw_body[:200]}")
            raise HTTPException(status_code=400, detail="Incomplete event data")
        
        lock_key = f"lock:webhook:{event.id}"
        try:
            acquired = await redis.set(lock_key, "1", nx=True, ex=30)
        except Exception as e:
            logger.error(f"WEBHOOK_DODO_REDIS_LOCK_FAILED: {e}")
            raise HTTPException(status_code=500, detail="Redis lock failure")

        if not acquired:
            logger.warning(f"WEBHOOK_DODO_CONCURRENT_ATTEMPT: {event.id}")
            raise HTTPException(status_code=409, detail="Concurrent processing")

        try:
            async with db.begin() as conn:
                try:
                    is_processed = await conn.execute(
                        text("SELECT 1 FROM processed_events WHERE event_id = :eid"),
                        {"eid": event.id}
                    )
                    if is_processed.fetchone():
                        logger.info(f"WEBHOOK_DODO_ALREADY_PROCESSED: {event.id}")
                        return {"status": "ok"}
                except Exception as e:
                    logger.error(f"WEBHOOK_DODO_DB_CHECK_FAILED: {e}")
                    raise HTTPException(status_code=500, detail="Database check failure")

                try:
                    purchase_res = await conn.execute(
                        text("""
                            INSERT INTO purchases (email, plan, provider_event_id, provider, status)
                            VALUES (:email, :plan, :eid, 'dodo', 'paid')
                            RETURNING id
                        """),
                        {"email": event.email, "plan": event.plan, "eid": event.id}
                    )
                    purchase_id = purchase_res.scalar()
                except Exception as e:
                    logger.error(f"WEBHOOK_DODO_DB_INSERT_FAILED: {e}")
                    raise HTTPException(status_code=500, detail="Database insert failure")
                
                try:
                    await conn.execute(
                        text("INSERT INTO processed_events (event_id) VALUES (:eid)"),
                        {"eid": event.id}
                    )
                except Exception as e:
                    logger.error(f"WEBHOOK_DODO_DB_IDEMPOTENCY_FAILED: {e}")
                
            raw_token = secrets.token_urlsafe(32)
            token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
            
            redis_payload = json.dumps({
                "email": event.email,
                "plan": event.plan,
                "purchase_id": str(purchase_id)
            })
            try:
                await redis.set(f"magic:{token_hash}", redis_payload, ex=1800)
            except Exception as e:
                logger.error(f"WEBHOOK_DODO_REDIS_STORE_FAILED: {e}")
                raise HTTPException(status_code=500, detail="Redis storage failure")
            
            app_origin = settings.APP_ORIGIN or "http://localhost:8000"
            magic_url = f"{app_origin}/api/auth/magic?token={raw_token}"
            
            try:
                await email_service.send_magic_link(
                    email=event.email,
                    magic_link=magic_url,
                    expire_minutes=30
                )
            except Exception as e:
                logger.error(f"WEBHOOK_DODO_EMAIL_SEND_FAILED: {e}")
            
        finally:
            try:
                await redis.delete(lock_key)
            except Exception as e:
                logger.error(f"WEBHOOK_DODO_REDIS_UNLOCK_FAILED: {e}")

    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"WEBHOOK_DODO_CRITICAL_FAILURE: {e}")
        raise HTTPException(status_code=500, detail="Critical webhook failure")

    return {"status": "ok"}
