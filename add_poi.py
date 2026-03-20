import asyncio
from sqlalchemy import text
from app.main import build_async_engine

async def add_poi():
    engine = build_async_engine()
    async with engine.begin() as conn:
        await conn.execute(
            text("""
                INSERT INTO pois (name, geom) 
                VALUES (:name, ST_SetSRID(ST_MakePoint(:lon, :lat), 4326)::geography)
            """),
            {'name': 'Hiyori Garden Tower', 'lat': 16.0613474, 'lon': 108.2357775}
        )
    await engine.dispose()
    print('OK POI "Hiyori Garden Tower" added successfully to pois table')

if __name__ == '__main__':
    asyncio.run(add_poi())
