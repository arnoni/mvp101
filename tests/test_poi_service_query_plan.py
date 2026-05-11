"""Verify the tier-based distance query uses the GiST index on pois.geom."""

import os

import pytest

sqlalchemy = pytest.importorskip("sqlalchemy")
sqlalchemy_asyncio = pytest.importorskip("sqlalchemy.ext.asyncio")
text = sqlalchemy.text
create_async_engine = sqlalchemy_asyncio.create_async_engine


@pytest.mark.asyncio
async def test_distance_query_uses_idx_pois_geom_index_scan():
    """EXPLAIN must show idx_pois_geom and must not show a sequential scan."""
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        pytest.skip("DATABASE_URL is not configured for query-plan integration test")

    from app.core.db import build_asyncpg_url_and_connect_args

    url, connect_args = build_asyncpg_url_and_connect_args(database_url)
    engine = create_async_engine(url, connect_args=connect_args)
    user_lat, user_lon = 16.048792, 108.240859

    try:
        async with engine.begin() as conn:
            # Force the planner away from a sequential scan on tiny/empty dev tables
            # so this test validates the ST_DWithin expression is indexable.
            await conn.execute(text("SET LOCAL enable_seqscan = off"))
            result = await conn.execute(
                text(
                    """
                    EXPLAIN (FORMAT TEXT)
                    SELECT COUNT(*)
                    FROM pois
                    WHERE ST_DWithin(
                        geom,
                        ST_SetSRID(ST_MakePoint(:lon, :lat), 4326)::geography,
                        40000
                    )
                    """
                ),
                {"lat": user_lat, "lon": user_lon},
            )
            plan_text = "\n".join(str(row[0]) for row in result.fetchall())
    finally:
        await engine.dispose()

    assert "idx_pois_geom" in plan_text, (
        f"Query plan does not use idx_pois_geom index:\n{plan_text}"
    )
    assert "Index Scan" in plan_text or "Index Only Scan" in plan_text, (
        f"Query plan does not use index scan:\n{plan_text}"
    )
    assert "Seq Scan" not in plan_text, (
        f"Query plan uses sequential scan instead of index:\n{plan_text}"
    )
