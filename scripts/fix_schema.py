
import asyncio
import os
import sys

# Ensure project root is in path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy import text
from app.core.config import settings

async def fix_schema():
    print(f"Connecting to database: {settings.DATABASE_URL}")
    if not settings.DATABASE_URL:
        print("DATABASE_URL not set.")
        return

    url = settings.DATABASE_URL.replace("postgresql://", "postgresql+asyncpg://", 1)
    if "sslmode=" not in url:
        url += "&sslmode=require" if "?" in url else "?sslmode=require"
    
    engine = create_async_engine(url, echo=True)

    async with engine.begin() as conn:
        print("Dropping old cell_poi_precompute table...")
        await conn.execute(text("DROP TABLE IF EXISTS cell_poi_precompute CASCADE;"))
        
        print("Creating new cell_poi_precompute/data schema...")
        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS cell_poi_precompute (
                cell_id TEXT PRIMARY KEY,
                data JSONB NOT NULL,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
            );
        """))
    
    print("Schema fix complete.")
    await engine.dispose()

if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(fix_schema())
