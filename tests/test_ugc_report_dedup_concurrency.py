import asyncio
from types import SimpleNamespace

import pytest

from app.api.routes import UGCReportRequest, ugc_report_submit
from app.schemas.user_reports import ReportType
from app.services.entitlement_service import TierStatus


class AtomicFakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.lock = asyncio.Lock()

    async def set(self, key: str, value: str, *, ex: int | None = None, nx: bool = False):
        async with self.lock:
            if nx and key in self.values:
                return False
            self.values[key] = value
            return True

    async def get(self, key: str):
        return self.values.get(key)

    async def delete(self, key: str):
        self.values.pop(key, None)


class FakeResult:
    def __init__(self, public_id: str) -> None:
        self.public_id = public_id

    def first(self):
        return SimpleNamespace(public_id=self.public_id)


class FakeConnection:
    def __init__(self, engine: "FakeEngine") -> None:
        self.engine = engine

    async def execute(self, sql, params):
        async with self.engine.lock:
            self.engine.insert_count += 1
        return FakeResult(params["public_id"])


class FakeBegin:
    def __init__(self, engine: "FakeEngine") -> None:
        self.engine = engine

    async def __aenter__(self):
        return FakeConnection(self.engine)

    async def __aexit__(self, exc_type, exc, tb):
        return False


class FakeEngine:
    def __init__(self) -> None:
        self.insert_count = 0
        self.lock = asyncio.Lock()

    def begin(self):
        return FakeBegin(self)


@pytest.mark.asyncio
async def test_ugc_report_submit_deduplicates_50_concurrent_identical_reports():
    redis = AtomicFakeRedis()
    db_engine = FakeEngine()
    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(redis=redis, db_engine=db_engine)),
        state=SimpleNamespace(anon_id="anon-1", user_id=None, tier=TierStatus.FREE),
    )
    data = UGCReportRequest(
        title="Construction noise",
        description="Heavy equipment operating next door",
        lat=16.0544,
        lon=108.2022,
        report_type=ReportType.ACTIVE,
        evidence_urls=["https://example.com/photo.jpg"],
    )

    responses = await asyncio.gather(
        *(ugc_report_submit(request, data, quota_repo=None, policy_engine=None) for _ in range(50))
    )

    assert db_engine.insert_count == 1
    assert sum(1 for response in responses if response["duplicate"] is False) == 1
    assert sum(1 for response in responses if response["duplicate"] is True) == 49
