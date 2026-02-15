import hashlib
import secrets
import uuid
from typing import Optional
from datetime import datetime, timedelta, timezone
from sqlalchemy.ext.asyncio import AsyncEngine
from sqlalchemy import text
from app.core.config import settings

class MagicLinkService:
    def __init__(self, engine: AsyncEngine):
        self.engine = engine

    def _hash_token(self, token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()

    async def create_magic_link(self, email: str, purpose: str = "login") -> str:
        """
        Creates a magic link token, stores it, and returns the RAW token.
        The raw token is NEVER stored.
        """
        raw_token = secrets.token_urlsafe(settings.MAGICLINK_TOKEN_BYTES)
        token_hash = self._hash_token(raw_token)
        expires_at = datetime.now(timezone.utc) + timedelta(minutes=settings.MAGICLINK_EXPIRY_MINUTES)
        
        sql = text("""
            INSERT INTO magic_link_tokens (email, token_hash, purpose, expires_at)
            VALUES (:email, :token_hash, :purpose, :expires_at)
            RETURNING id
        """)
        
        async with self.engine.begin() as conn:
            await conn.execute(
                sql, 
                {
                    "email": email, 
                    "token_hash": token_hash, 
                    "purpose": purpose, 
                    "expires_at": expires_at
                }
            )
            
        return raw_token

    async def redeem_token(self, raw_token: str) -> Optional[str]:
        """
        Validates a token. If valid, marks as redeemed and returns the email.
        """
        token_hash = self._hash_token(raw_token)
        now = datetime.now(timezone.utc)
        
        sql_select = text("""
            SELECT id, email, expires_at, redeemed_at 
            FROM magic_link_tokens 
            WHERE token_hash = :token_hash
        """)
        
        sql_update = text("""
            UPDATE magic_link_tokens 
            SET redeemed_at = :now 
            WHERE id = :id
        """)
        
        async with self.engine.begin() as conn:
            result = await conn.execute(sql_select, {"token_hash": token_hash})
            row = result.fetchone()
            
            if not row:
                return None # Invalid default
                
            token_id, email, expires_at, redeemed_at = row
            
            if redeemed_at:
                return None # Already used
            if expires_at < now:
                return None # Expired
                
            # Mark redeemed
            await conn.execute(sql_update, {"now": now, "id": token_id})
            
            # Ensure user exists
            await conn.execute(
                text("INSERT INTO users (email) VALUES (:email) ON CONFLICT (email) DO NOTHING"),
                {"email": email}
            )
            
            # Fetch User ID
            user_res = await conn.execute(
                text("SELECT id FROM users WHERE email = :email"),
                {"email": email}
            )
            user_id = user_res.scalar()
            
            return str(user_id)
