"""预留 heartbeat 与过期回收状态机单元测试。"""
import asyncio
from contextlib import asynccontextmanager
from datetime import timedelta

from app.services import storage_quota as sq
from models import StorageQuotaReservation
from tests.test_storage_quota import FakeOperationLock, Memo, _install, _mk


class _Scalars:
    def __init__(self, values):
        self.values = values

    def all(self):
        return self.values


class _Result:
    def __init__(self, values):
        self.values = values

    def scalars(self):
        return _Scalars(self.values)


class _CandidateSession:
    def __init__(self, base, candidates):
        self.base = base
        self.candidates = candidates
        self.memo = base.memo

    async def execute(self, _stmt):
        return _Result(self.candidates)

    async def commit(self):
        return await self.base.commit()

    def add(self, value):
        return self.base.add(value)

    def begin_nested(self):
        return self.base.begin_nested()

    async def flush(self):
        return await self.base.flush()


def test_touch_extends_only_reserved(monkeypatch):
    memo = Memo()
    mod, session = _install(monkeypatch, memo, enabled=True, dry_run=False)
    now = sq.now_utc()
    active = StorageQuotaReservation(
        reservation_id="active", user_id=1, kind="training", sample_key="a",
        status="reserved", requested_bytes=1, expires_at=now,
    )
    settled = StorageQuotaReservation(
        reservation_id="settled", user_id=1, kind="training", sample_key="b",
        status="settled", requested_bytes=1, expires_at=now,
    )
    memo.reservations.update(active=active, settled=settled)

    async def run():
        ok = await mod.touch_reservation(
            session, reservation_id="active", extend_minutes=5,
            lock_context=FakeOperationLock(),
        )
        no = await mod.touch_reservation(
            session, reservation_id="settled", extend_minutes=5,
            lock_context=FakeOperationLock(),
        )
        return ok, no

    ok, no = asyncio.run(run())
    assert ok is True and no is False
    assert active.expires_at > now + timedelta(minutes=4)
    assert settled.expires_at == now


def test_reclaim_rechecks_under_combination_lock_and_marks_needed(monkeypatch):
    memo = Memo()
    mod, base = _install(monkeypatch, memo, enabled=True, dry_run=False)
    now = sq.now_utc()
    quota = _mk(1, "training", reserved=10)
    memo.quotas[(1, "training")] = quota
    expired = StorageQuotaReservation(
        reservation_id="expired", user_id=1, kind="training", sample_key="s",
        status="reserved", requested_bytes=10,
        expires_at=now - timedelta(minutes=1),
    )
    memo.reservations["expired"] = expired
    session = _CandidateSession(base, [expired])
    locks = []

    @asynccontextmanager
    async def operation_lock(user_id, kind, sample_key, timeout=2.0):
        locks.append((user_id, kind, sample_key))
        yield FakeOperationLock()

    monkeypatch.setattr(mod, "quota_operation_lock", operation_lock)
    result = asyncio.run(mod.reclaim_expired_reservations(session, now=now))

    assert result.processed == 1
    assert result.failed == 0
    assert locks == [(1, "training", "s")]
    assert expired.status == "expired"
    assert quota.reserved_bytes == 0
    assert quota.reconcile_needed is True
    assert quota.reconcile_reason == "reservation_expired"
    assert quota.reconcile_sample_key == "s"


def test_reclaim_skips_reservation_extended_after_snapshot(monkeypatch):
    memo = Memo()
    mod, base = _install(monkeypatch, memo, enabled=True, dry_run=False)
    now = sq.now_utc()
    row = StorageQuotaReservation(
        reservation_id="r", user_id=1, kind="training", sample_key="s",
        status="reserved", requested_bytes=10,
        expires_at=now - timedelta(minutes=1),
    )
    memo.reservations["r"] = row
    session = _CandidateSession(base, [row])

    @asynccontextmanager
    async def operation_lock(*_args, **_kwargs):
        row.expires_at = now + timedelta(minutes=10)
        yield FakeOperationLock()

    monkeypatch.setattr(mod, "quota_operation_lock", operation_lock)
    result = asyncio.run(mod.reclaim_expired_reservations(session, now=now))

    assert result.processed == 0
    assert result.skipped == 1
    assert row.status == "reserved"
