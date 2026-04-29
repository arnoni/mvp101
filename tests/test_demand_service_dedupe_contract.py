from pathlib import Path


def test_demand_service_record_query_supports_actor_dedupe_window():
    demand_service_py = Path("app/services/demand_service.py").read_text(encoding="utf-8")
    assert "async def record_query(self, cell_id: str, actor_key: str | None = None, dedupe_window_seconds: int = 3600) -> bool:" in demand_service_py
    assert "dedupe_key = f\"dd:demand_dedupe:{actor_key}:{cell_id}:{bucket}\"" in demand_service_py
    assert "claimed = await self.redis.set(dedupe_key, \"1\", ex=dedupe_window_seconds, nx=True)" in demand_service_py
    assert "if not claimed:" in demand_service_py
    assert "return False" in demand_service_py
