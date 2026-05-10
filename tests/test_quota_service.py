from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace

import pytest

from app.services.quota_service import (
    compute_construction_fingerprint,
    consume_construction_credit,
    get_or_initialize_remaining_quota,
    has_construction_query,
)


class _Result:
    def __init__(self, row=None):
        self._row = row

    def first(self):
        return self._row


class FakeQuotaDB:
    def __init__(self, *, remaining=None):
        self.user_id = uuid.uuid4()
        self.remaining = remaining
        self.fingerprints: set[str] = set()
        self.commits = 0
        self.rollbacks = 0
        self.insert_count = 0

    async def execute(self, sql, params=None):
        statement = str(sql).lower()
        params = params or {}
        fingerprint = params.get("fingerprint")
        if "from construction_queries" in statement:
            return _Result(SimpleNamespace(found=1) if fingerprint in self.fingerprints else None)
        if "select remaining_quota" in statement and "for update" in statement:
            return _Result(SimpleNamespace(remaining_quota=self.remaining))
        if "insert into construction_queries" in statement:
            if fingerprint not in self.fingerprints:
                self.insert_count += 1
            self.fingerprints.add(fingerprint)
            return _Result()
        if "set remaining_quota = remaining_quota - 1" in statement:
            if self.remaining is None or self.remaining <= 0:
                return _Result(None)
            self.remaining -= 1
            return _Result(SimpleNamespace(remaining_quota=self.remaining))
        if "when remaining_quota is null then :daily_limit" in statement:
            if self.remaining is None:
                self.remaining = int(params["daily_limit"])
            return _Result(SimpleNamespace(remaining_quota=self.remaining))
        if "set remaining_quota = :daily_limit" in statement:
            self.remaining = int(params["daily_limit"])
            return _Result()
        raise AssertionError(f"unexpected SQL: {statement}")

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        self.rollbacks += 1


@pytest.mark.asyncio
async def test_initializes_remaining_quota_for_authenticated_free_user():
    db = FakeQuotaDB(remaining=None)

    remaining = await get_or_initialize_remaining_quota(db=db, user_id=db.user_id, daily_limit=3)

    assert remaining == 3
    assert db.remaining == 3
    assert db.commits == 1


@pytest.mark.asyncio
async def test_initializes_remaining_quota_for_simulated_paid_user():
    db = FakeQuotaDB(remaining=None)

    remaining = await get_or_initialize_remaining_quota(db=db, user_id=db.user_id, daily_limit=5)

    assert remaining == 5
    assert db.remaining == 5
    assert db.commits == 1


@pytest.mark.asyncio
async def test_initializes_remaining_quota_for_real_paid_user():
    db = FakeQuotaDB(remaining=None)

    remaining = await get_or_initialize_remaining_quota(db=db, user_id=db.user_id, daily_limit=10)

    assert remaining == 10
    assert db.remaining == 10
    assert db.commits == 1


@pytest.mark.asyncio
async def test_consumes_one_credit_for_new_construction_query():
    db = FakeQuotaDB(remaining=5)
    fingerprint = compute_construction_fingerprint(16.019944, 108.254861, 50)

    result = await consume_construction_credit(
        db=db,
        user_id=db.user_id,
        daily_limit=5,
        query_fingerprint=fingerprint,
    )

    assert result.consumed is True
    assert result.remaining_quota == 4
    assert result.reason == "new_construction_query"
    assert fingerprint in db.fingerprints


@pytest.mark.asyncio
async def test_does_not_consume_credit_for_duplicate_construction_fingerprint():
    db = FakeQuotaDB(remaining=4)
    fingerprint = compute_construction_fingerprint(16.019944, 108.254861, 50)
    db.fingerprints.add(fingerprint)

    result = await consume_construction_credit(
        db=db,
        user_id=db.user_id,
        daily_limit=5,
        query_fingerprint=fingerprint,
    )

    assert result.consumed is False
    assert result.remaining_quota == 4
    assert result.reason == "duplicate_construction_query_no_charge"


@pytest.mark.asyncio
async def test_insufficient_quota_returns_correct_result_and_never_negative():
    db = FakeQuotaDB(remaining=0)

    result = await consume_construction_credit(
        db=db,
        user_id=db.user_id,
        daily_limit=5,
        query_fingerprint="abc",
    )

    assert result.consumed is False
    assert result.remaining_quota == 0
    assert result.reason == "insufficient_quota"
    assert db.remaining == 0
    assert db.fingerprints == set()


@pytest.mark.asyncio
async def test_has_construction_query_tracks_prior_construction():
    db = FakeQuotaDB(remaining=2)
    db.fingerprints.add("abc")

    assert await has_construction_query(db=db, user_id=db.user_id, query_fingerprint="abc") is True
    assert await has_construction_query(db=db, user_id=db.user_id, query_fingerprint="def") is False


@pytest.mark.asyncio
async def test_both_target_consumes_one_credit_not_two():
    db = FakeQuotaDB(remaining=5)
    fingerprint = compute_construction_fingerprint(16.019944, 108.254861, 50)

    first = await consume_construction_credit(
        db=db,
        user_id=db.user_id,
        daily_limit=5,
        query_fingerprint=fingerprint,
    )
    second = await consume_construction_credit(
        db=db,
        user_id=db.user_id,
        daily_limit=5,
        query_fingerprint=fingerprint,
    )

    assert first.consumed is True
    assert first.remaining_quota == 4
    assert second.consumed is False
    assert second.remaining_quota == 4
    assert second.reason == "duplicate_construction_query_no_charge"
    assert db.insert_count == 1


@pytest.mark.asyncio
async def test_does_not_allow_remaining_quota_below_zero():
    db = FakeQuotaDB(remaining=0)

    result = await consume_construction_credit(
        db=db,
        user_id=db.user_id,
        daily_limit=5,
        query_fingerprint="abc",
    )

    assert result.consumed is False
    assert result.reason == "insufficient_quota"
    assert result.remaining_quota == 0
    assert db.remaining == 0
    assert db.fingerprints == set()
    assert db.insert_count == 0


@pytest.mark.asyncio
async def test_concurrent_same_fingerprint_consumes_once_and_never_negative():
    db = FakeQuotaDB(remaining=1)
    fingerprint = compute_construction_fingerprint(16.019944, 108.254861, 50)

    results = await asyncio.gather(
        *(
            consume_construction_credit(
                db=db,
                user_id=db.user_id,
                daily_limit=1,
                query_fingerprint=fingerprint,
            )
            for _ in range(20)
        )
    )

    consumed = [result for result in results if result.consumed]
    not_consumed = [result for result in results if not result.consumed]

    assert len(consumed) == 1
    assert len(not_consumed) == 19
    assert {result.reason for result in not_consumed} <= {
        "duplicate_construction_query_no_charge",
        "insufficient_quota",
    }
    assert db.remaining == 0
    assert len(db.fingerprints) == 1
    assert db.insert_count == 1
