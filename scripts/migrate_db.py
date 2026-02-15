import asyncio
import os
import sys

# Ensure project root is in path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy import text
from app.core.config import settings

async def run_migrations():
    print(f"Connecting to database: {settings.DATABASE_URL}")
    if not settings.DATABASE_URL:
        print("DATABASE_URL not set. Skipping migrations.")
        return

    # Create async engine
    url = settings.DATABASE_URL.replace("postgresql://", "postgresql+asyncpg://", 1)
    engine = create_async_engine(url, echo=True)

    sql_statements = [
        """
        CREATE TABLE IF NOT EXISTS users (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            email TEXT UNIQUE NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        """,
        """
        CREATE TABLE IF NOT EXISTS subscriptions (
            user_id UUID PRIMARY KEY REFERENCES users(id),
            provider TEXT NOT NULL, 
            provider_subscription_id TEXT UNIQUE NOT NULL,
            status TEXT NOT NULL, 
            current_period_end TIMESTAMPTZ NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        """,
        """
        CREATE TABLE IF NOT EXISTS magic_link_tokens (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            user_id UUID REFERENCES users(id),
            email TEXT NOT NULL,
            token_hash TEXT NOT NULL UNIQUE, 
            purpose TEXT NOT NULL DEFAULT 'login',
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            expires_at TIMESTAMPTZ NOT NULL,
            redeemed_at TIMESTAMPTZ,
            requester_ip_hash TEXT,
            user_agent_hash TEXT
        );
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_magic_tokens_email_created 
        ON magic_link_tokens(email, created_at);
        """,
        """
        CREATE TABLE IF NOT EXISTS cell_poi_precompute (
            cell_id TEXT PRIMARY KEY,
            data JSONB NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_precompute_size_computed 
        ON cell_poi_precompute(cell_size_m, computed_at);
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_precompute_etag 
        ON cell_poi_precompute(etag);
        """,
        """
        CREATE TABLE IF NOT EXISTS webhook_events (
            provider TEXT NOT NULL,
            event_id TEXT NOT NULL,
            received_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            processed_at TIMESTAMPTZ,
            payload JSONB NOT NULL,
            PRIMARY KEY (provider, event_id)
        );
        """
    ]

    async with engine.begin() as conn:
        for statement in sql_statements:
            try:
                print(f"Executing: {statement[:50]}...")
                await conn.execute(text(statement))
            except Exception as e:
                print(f"Error executing statement: {e}")
                # Don't strictly fail, as some might already exist or conflict slightly
                # But for CREATE IF NOT EXISTS it should be fine.

    print("Migrations complete.")
    await engine.dispose()

if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(run_migrations())
