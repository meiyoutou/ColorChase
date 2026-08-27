"""阶段 B1：reserve_quota 的组合锁所有权契约。"""
import asyncio
import inspect
from types import SimpleNamespace

import pytest

from app.services import storage_quota as sq


class _Session:
    async def commit(self):
        return None


class _OperationLock:
    def __init__(self, acquired=True):
        self.acquired = acquired
        self.row_token = object() if acquired else None
        self.sample_token = object() if acquired else None


def test_reserve_quota_uses_lock_context_without_reacquiring(monkeypatch):
    calls = []

    async def explode_row_lock(**_kwargs):
        calls.append("row")
        raise AssertionError("row lock must not be reacquired")

    async def fake_locked(*_args, **kwargs):
        assert kwargs["has_lock"] is True
        return sq.ReserveOutcome(allowed=True)

    monkeypatch.setattr(sq, "STORAGE_QUOTA_ENABLED", True)
    monkeypatch.setattr(sq, "STORAGE_QUOTA_DRY_RUN", False)
    monkeypatch.setattr(sq, "acquire_quota_row_lock", explode_row_lock)
    monkeypatch.setattr(sq, "_reserve_quota_locked", fake_locked)

    async def run():
        return await sq.reserve_quota(
            _Session(), user_id=1, role="user", kind="training",
            sample_key="sample", incoming_bytes=1,
            lock_context=_OperationLock(True),
        )

    outcome = asyncio.run(run())
    assert outcome.allowed is True
    assert calls == []


def test_reserve_quota_unavailable_context_raises_in_force_mode(monkeypatch):
    monkeypatch.setattr(sq, "STORAGE_QUOTA_ENABLED", True)
    monkeypatch.setattr(sq, "STORAGE_QUOTA_DRY_RUN", False)

    async def run():
        await sq.reserve_quota(
            _Session(), user_id=1, role="user", kind="training",
            sample_key="sample", incoming_bytes=1,
            lock_context=_OperationLock(False),
        )

    with pytest.raises(sq.QuotaLockUnavailable):
        asyncio.run(run())


def test_reserve_quota_without_context_uses_and_releases_row_lock(monkeypatch):
    events = []

    class Token:
        acquired = True

        async def release(self):
            events.append("release")
            self.acquired = False

    async def acquire(**_kwargs):
        events.append("acquire")
        return Token()

    async def fake_locked(*_args, **_kwargs):
        events.append("reserve")
        return sq.ReserveOutcome(allowed=True)

    monkeypatch.setattr(sq, "STORAGE_QUOTA_ENABLED", True)
    monkeypatch.setattr(sq, "STORAGE_QUOTA_DRY_RUN", False)
    monkeypatch.setattr(sq, "acquire_quota_row_lock", acquire)
    monkeypatch.setattr(sq, "_reserve_quota_locked", fake_locked)

    asyncio.run(sq.reserve_quota(
        _Session(), user_id=1, role="user", kind="training",
        sample_key="sample", incoming_bytes=1,
    ))

    assert events == ["acquire", "reserve", "release"]


def test_reserve_quota_no_longer_accepts_sample_only_lock_token():
    assert "lock_context" in inspect.signature(sq.reserve_quota).parameters
    assert "lock_token" not in inspect.signature(sq.reserve_quota).parameters


def test_reserve_outcome_declares_dry_run_fail_open():
    outcome = sq.ReserveOutcome(allowed=True, dry_run_fail_open=True)
    assert outcome.dry_run_fail_open is True
